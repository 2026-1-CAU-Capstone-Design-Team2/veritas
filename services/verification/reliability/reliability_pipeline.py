"""Top-level pipeline entry point for the reliability verify task.

Composes:

1. :func:`batch_index.build_batch_index` — parse every ``summary/batch_*.md``
   for inline ``[doc_<id>]`` markers and group findings by source doc.
2. :func:`llm_judge.judge_documents` — call the LLM in fixed-size batches
   (default 5 docs / call) to get one structured verdict per doc.
3. Inherit verdicts for ``DocRecord.is_duplicate`` docs from their source so
   duplicates do not consume LLM budget but still appear in the result.

The output dataclasses are defined here (not in ``models.py``) because the
reliability artifact is logically self-contained — adding them to the shared
models module would couple legacy tasks to a brand-new schema for no benefit.
``persistence.py`` imports them through ``services.verification.reliability``
so the public import surface stays clean.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import DocRecord
from .batch_index import BatchMention, build_batch_index
from .llm_judge import judge_documents


@dataclass
class ReliabilityMentionDTO:
    """Persisted form of a :class:`batch_index.BatchMention`.

    Kept separate from ``BatchMention`` so the on-disk schema can evolve
    without dragging the parser's internal representation along.
    """

    batch_id: str
    kind: str
    snippet: str


@dataclass
class ReliabilityItem:
    """One per-doc verdict — the unit the verify UI card renders."""

    doc_id: str
    level: str  # "high" | "medium" | "low"
    rationale: str
    signals: dict[str, str] = field(default_factory=dict)
    batch_mentions: list[ReliabilityMentionDTO] = field(default_factory=list)
    inherited_from: str | None = None  # set for duplicate-doc inheritance


@dataclass
class ReliabilityResult:
    """Task output — every doc's verdict plus a workspace-level distribution."""

    items: list[ReliabilityItem] = field(default_factory=list)
    distribution: dict[str, int] = field(default_factory=dict)


def _mention_dto(mention: BatchMention) -> ReliabilityMentionDTO:
    return ReliabilityMentionDTO(
        batch_id=mention.batch_id,
        kind=mention.kind,
        snippet=mention.snippet,
    )


def _distribution(items: list[ReliabilityItem]) -> dict[str, int]:
    """``{"high": N, "medium": M, "low": K}`` headline counts."""
    counts = {"high": 0, "medium": 0, "low": 0}
    for item in items:
        if item.level in counts:
            counts[item.level] += 1
    return counts


def _judge_eligible_docs(docs: list[DocRecord]) -> list[DocRecord]:
    """Skip duplicates — they inherit from the source via ``duplicate_of``.

    A duplicate has no clean_md / no key_points / no reliability_notes; sending
    it to the LLM wastes a call and yields a uniformly low verdict that lies
    about the underlying content.
    """
    return [doc for doc in docs if not doc.is_duplicate]


def _inherit_for_duplicates(
    docs: list[DocRecord],
    judged: dict[str, ReliabilityItem],
    mentions_by_doc: dict[str, list[BatchMention]],
) -> list[ReliabilityItem]:
    """Build verdicts for duplicate docs by copying from ``duplicate_of``.

    When the original isn't itself in the kept doc set (rare — usually means
    duplicate_of points to a doc that was filtered out as invalid), we fall
    back to a neutral medium verdict that names the missing source so the
    user can act on it.
    """
    inherited: list[ReliabilityItem] = []
    for doc in docs:
        if not doc.is_duplicate:
            continue
        source_id = doc.duplicate_of or ""
        source_item = judged.get(source_id)
        if source_item is not None:
            inherited.append(
                ReliabilityItem(
                    doc_id=doc.doc_id,
                    level=source_item.level,
                    rationale=(
                        f"원본 자료(doc_{source_id})와 동일 컨텐츠로 식별되어"
                        " 동일 등급이 적용되었습니다."
                    ),
                    signals=dict(source_item.signals),
                    batch_mentions=[
                        _mention_dto(m) for m in mentions_by_doc.get(doc.doc_id, [])
                    ],
                    inherited_from=source_id,
                )
            )
        else:
            inherited.append(
                ReliabilityItem(
                    doc_id=doc.doc_id,
                    level="medium",
                    rationale=(
                        "중복 표시된 자료지만 원본 자료를 찾지 못해 임시 등급을 적용했습니다."
                    ),
                    signals={
                        "authority": "mixed",
                        "verifiability": "mixed",
                        "self_consistency": "mixed",
                    },
                    batch_mentions=[
                        _mention_dto(m) for m in mentions_by_doc.get(doc.doc_id, [])
                    ],
                    inherited_from=source_id or None,
                )
            )
    return inherited


def run_reliability_pipeline(
    docs: list[DocRecord],
    *,
    llm: Any,
    summary_dir: str | Path,
    request_text: str = "",
    batch_size: int = 5,
) -> ReliabilityResult:
    """Run the full reliability task end-to-end.

    ``summary_dir`` is the path to ``runs/<ws>/summary/`` — where the
    ``batch_*.md`` files live. The pipeline is pure with respect to ``docs``;
    it does not re-load them from disk.
    """
    mentions_by_doc = build_batch_index(summary_dir)

    eligible = _judge_eligible_docs(docs)
    verdicts = judge_documents(
        eligible,
        llm=llm,
        mentions_by_doc=mentions_by_doc,
        request_text=request_text,
        batch_size=batch_size,
    )

    items: list[ReliabilityItem] = []
    judged_by_id: dict[str, ReliabilityItem] = {}
    for verdict in verdicts:
        item = ReliabilityItem(
            doc_id=verdict.doc_id,
            level=verdict.level,
            rationale=verdict.rationale,
            signals=dict(verdict.signals),
            batch_mentions=[
                _mention_dto(m) for m in mentions_by_doc.get(verdict.doc_id, [])
            ],
            inherited_from=None,
        )
        items.append(item)
        judged_by_id[item.doc_id] = item

    items.extend(_inherit_for_duplicates(docs, judged_by_id, mentions_by_doc))
    # Stable ordering by doc_id so persisted JSON diffs cleanly across reruns.
    items.sort(key=lambda i: i.doc_id)

    return ReliabilityResult(items=items, distribution=_distribution(items))


__all__ = [
    "ReliabilityItem",
    "ReliabilityMentionDTO",
    "ReliabilityResult",
    "run_reliability_pipeline",
]
