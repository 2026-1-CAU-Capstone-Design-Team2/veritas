from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

from core.knowledge_models import KnowledgeSourceRecord, SourceScope
from core.models import ParsedDocRecord
from core.verification_crosscheck_models import (
    CrossCheckArtifact,
    CrossCheckClaim,
    CrossCheckRelation,
)

_NUMBER_RE = re.compile(r"(?<![\w.])-?\d+(?:,\d{3})*(?:\.\d+)?%?")
_TOKEN_RE = re.compile(r"[A-Za-z0-9\uac00-\ud7a3][A-Za-z0-9\uac00-\ud7a3_-]{1,}")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?\u3002\uff1f\uff01])\s+|\n+")


def run_crosscheck_pipeline(
    *,
    external_docs: list[ParsedDocRecord],
    local_sources: list[KnowledgeSourceRecord],
    local_documents: dict[str, str],
    max_claims_per_source: int = 8,
) -> CrossCheckArtifact:
    external_claims = _claims_from_external(external_docs, max_claims_per_source)
    local_claims = _claims_from_local(local_sources, local_documents, max_claims_per_source)
    claims = external_claims + local_claims
    relations: list[CrossCheckRelation] = []
    flags: list[dict] = []

    for external in external_claims:
        for local in local_claims:
            relation = _compare_claims(external, local)
            if relation is None:
                continue
            relations.append(relation)
            if relation.relation in {"numeric_mismatch", "contradicts"}:
                flags.append(
                    {
                        "relation": relation.relation,
                        "severity": relation.severity,
                        "claimA": relation.claim_a,
                        "claimB": relation.claim_b,
                        "message": relation.reason,
                    }
                )

    return CrossCheckArtifact(claims=claims, relations=relations, flags=flags)


def _claims_from_external(
    docs: list[ParsedDocRecord],
    max_claims_per_source: int,
) -> list[CrossCheckClaim]:
    claims: list[CrossCheckClaim] = []
    for doc in docs:
        source_id = f"external:{doc.doc_id}"
        candidates = []
        candidates.extend(doc.key_points or [])
        if doc.summary:
            candidates.extend(_sentences(doc.summary))
        for index, text in enumerate(_dedupe(candidates)[:max_claims_per_source]):
            claims.append(
                CrossCheckClaim(
                    claim_id=f"{source_id}:claim_{index:03d}",
                    source_id=str(doc.doc_id),
                    source_scope=SourceScope.EXTERNAL,
                    text=text,
                    claim_type=_claim_type(text),
                    evidence_span=text,
                    metadata={"title": doc.title, "domain": doc.domain, "url": doc.url},
                )
            )
    return claims


def _claims_from_local(
    sources: list[KnowledgeSourceRecord],
    documents: dict[str, str],
    max_claims_per_source: int,
) -> list[CrossCheckClaim]:
    claims: list[CrossCheckClaim] = []
    for source in sources:
        text = documents.get(source.source_id, "")
        candidates = _sentences(text)
        for index, claim_text in enumerate(_dedupe(candidates)[:max_claims_per_source]):
            claims.append(
                CrossCheckClaim(
                    claim_id=f"local:{source.source_id}:claim_{index:03d}",
                    source_id=source.source_id,
                    source_scope=SourceScope.LOCAL,
                    text=claim_text,
                    claim_type=_claim_type(claim_text),
                    evidence_span=claim_text,
                    metadata={
                        "title": source.title,
                        "display_path": source.display_path,
                        "privacy_label": source.privacy_label.value,
                    },
                )
            )
    return claims


def _compare_claims(
    external: CrossCheckClaim,
    local: CrossCheckClaim,
) -> CrossCheckRelation | None:
    shared = _important_tokens(external.text) & _important_tokens(local.text)
    if len(shared) < 2:
        return None
    external_numbers = _numbers(external.text)
    local_numbers = _numbers(local.text)
    if external_numbers and local_numbers and external_numbers != local_numbers:
        return CrossCheckRelation(
            claim_a=external.claim_id,
            claim_b=local.claim_id,
            relation="numeric_mismatch",
            severity="high",
            reason=(
                "External and local claims discuss overlapping terms "
                f"({', '.join(sorted(shared)[:5])}) but cite different numbers: "
                f"external={sorted(external_numbers)}, local={sorted(local_numbers)}."
            ),
        )
    if external_numbers and local_numbers:
        return CrossCheckRelation(
            claim_a=external.claim_id,
            claim_b=local.claim_id,
            relation="supports",
            severity="low",
            reason="External and local claims share terms and numeric values.",
        )
    return CrossCheckRelation(
        claim_a=external.claim_id,
        claim_b=local.claim_id,
        relation="partially_supports",
        severity="low",
        reason="External and local claims share key terms; no numeric conflict was detected.",
    )


def _claim_type(text: str) -> str:
    if _NUMBER_RE.search(text):
        return "numeric"
    if re.search(r"\b(19|20)\d{2}\b", text):
        return "date"
    return "general"


def _sentences(text: str) -> list[str]:
    raw = _SENTENCE_SPLIT_RE.split(str(text or ""))
    sentences = []
    for item in raw:
        item = re.sub(r"\s+", " ", item).strip(" -*\t")
        if len(item) >= 30:
            sentences.append(item[:600])
    return sentences


def _dedupe(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _numbers(text: str) -> set[str]:
    return {match.group(0).replace(",", "") for match in _NUMBER_RE.finditer(text)}


def _important_tokens(text: str) -> set[str]:
    tokens = [token.lower() for token in _TOKEN_RE.findall(text)]
    counts = Counter(tokens)
    stop = {"the", "and", "for", "with", "this", "that", "from", "have", "has"}
    return {
        token
        for token, count in counts.items()
        if token not in stop and len(token) >= 3
    }


__all__ = ["run_crosscheck_pipeline"]
