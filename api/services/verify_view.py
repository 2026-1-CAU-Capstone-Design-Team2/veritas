"""Adapt the verification domain model to the frontend's UI schema.

Kept separate from ``verify_service.py`` so the *thin* API layer stays thin:
service.py owns the request flow (workspace resolution, persistence load,
progress polling), this module owns the *translation* into the user-facing
shape (``docId / title / matchRate / level / issues`` + detail breakdowns).

All non-technical phrasing — level labels, issue sentences — lives here. The
algorithm layer (``services/verification/``) never sees Korean labels or
percentile thresholds.
"""

from __future__ import annotations

from typing import Any

from services.verification.models import (
    ConflictFlag,
    ConsensusResult,
    IntentResult,
    SectionResult,
    VerificationArtifacts,
)

# Match-rate thresholds for the "신뢰도" badge. These are workspace-internal
# *rank percentiles* (0-100), not raw RRF scores, so the bands stay intuitive
# even when raw intent scores are tiny.
_LEVEL_HIGH = 70
_LEVEL_MEDIUM = 40


# ---------------------------------------------------------------------------
# Per-doc summary items (list view)
# ---------------------------------------------------------------------------


def build_doc_items(
    artifacts: VerificationArtifacts,
    doc_titles: dict[str, str],
) -> list[dict[str, Any]]:
    """One ranked item per doc — what the verify card list renders.

    ``doc_titles`` is the lookup the caller built from ``summary/index.json``;
    keeping it as an input keeps this module pure (no I/O).
    """
    intent: IntentResult | None = artifacts.intent
    sections: SectionResult | None = artifacts.sections
    consensus: ConsensusResult | None = artifacts.consensus

    intent_scores = dict(intent.doc_intent_score) if intent else {}
    rank_pct = rank_normalize_percent(intent_scores)
    doc_ids = sorted(set(rank_pct) | set(doc_titles))

    items: list[dict[str, Any]] = []
    for doc_id in doc_ids:
        percent = rank_pct.get(doc_id, 0)
        raw_intent = intent_scores.get(doc_id, 0.0)
        level = level_for(percent)
        items.append(
            {
                "docId": doc_id,
                "title": doc_titles.get(doc_id, f"문서 {doc_id}"),
                "matchRate": f"의도 일치율 {percent}%",
                "matchRatePercent": percent,
                "intentScore": round(float(raw_intent), 6),
                "level": level,
                "issues": _issues_for_doc(
                    doc_id=doc_id,
                    percent=percent,
                    intent=intent,
                    sections=sections,
                    consensus=consensus,
                ),
            }
        )

    items.sort(key=lambda item: -item["matchRatePercent"])
    return items


def rank_normalize_percent(scores: dict[str, float]) -> dict[str, int]:
    """Workspace-internal rank percentile (0-100). Ties share a percentile.

    Used because raw RRF scores are tiny (max ~0.017) and don't read well as a
    "match rate"; rank-normalizing inside the same workspace gives a 0-100
    scale that preserves order without faking absolute precision.
    """
    if not scores:
        return {}
    if len(scores) == 1:
        only = next(iter(scores))
        return {only: 100}

    ordered = sorted(scores.items(), key=lambda kv: kv[1])
    span = len(ordered) - 1
    out: dict[str, int] = {}
    last_score: float | None = None
    last_percent = 0
    for rank, (doc_id, score) in enumerate(ordered):
        if last_score is not None and abs(score - last_score) < 1e-9:
            percent = last_percent
        else:
            percent = int(round(rank / span * 100))
            last_percent = percent
            last_score = float(score)
        out[doc_id] = percent
    return out


def level_for(percent: int) -> str:
    if percent >= _LEVEL_HIGH:
        return "높음"
    if percent >= _LEVEL_MEDIUM:
        return "중간"
    return "낮음"


# ---------------------------------------------------------------------------
# Issues (short plain-Korean lines)
# ---------------------------------------------------------------------------


def _issues_for_doc(
    *,
    doc_id: str,
    percent: int,
    intent: IntentResult | None,
    sections: SectionResult | None,
    consensus: ConsensusResult | None,
) -> list[str]:
    """At most a handful of one-line notes about this document.

    Each helper appends 0-N lines; an empty list lets the UI fall back to
    "특이사항이 없습니다." without us having to special-case here.
    """
    notes: list[str] = []
    notes.extend(_intent_notes(doc_id, intent, percent))
    notes.extend(_section_notes(doc_id, sections))
    notes.extend(_consensus_notes(doc_id, consensus))
    return notes


def _intent_notes(
    doc_id: str,
    intent: IntentResult | None,
    percent: int,
) -> list[str]:
    if intent is None or intent.doc_facet_matrix is None or not intent.facets:
        return []
    try:
        column = intent.doc_order.index(doc_id)
    except ValueError:
        return []
    matrix = intent.doc_facet_matrix
    if column >= matrix.shape[1]:
        return []

    notes: list[str] = []
    facet_scores = matrix[:, column]
    if facet_scores.size == 0 or float(facet_scores.max()) <= 0.0:
        return notes

    # "Weak" = below 40% of this doc's own facet peak. Surface only the
    # weakest facet to keep the card readable.
    threshold = float(facet_scores.max()) * 0.4
    weak_rows = [
        row
        for row in range(len(intent.facets))
        if row < matrix.shape[0] and float(facet_scores[row]) <= threshold
    ]
    if weak_rows and percent < _LEVEL_HIGH:
        weakest = min(weak_rows, key=lambda r: float(facet_scores[r]))
        if 0 <= weakest < len(intent.facets):
            label = _format_label_list(intent.facets[weakest].label_terms)
            if label:
                notes.append(f"이 자료는 다음 주제를 충분히 다루지 않은 것으로 보입니다: {label}.")

    strong_index = int(facet_scores.argmax())
    if 0 <= strong_index < len(intent.facets):
        label = _format_label_list(intent.facets[strong_index].label_terms)
        if label:
            notes.append(f"이 자료가 가장 잘 다룬 주제: {label}.")
    return notes


def _section_notes(
    doc_id: str,
    sections: SectionResult | None,
) -> list[str]:
    if sections is None or not sections.sections:
        return []

    weak_labels: list[str] = []
    for section in sections.sections:
        if doc_id not in section.doc_scores:
            continue
        scores = sorted(section.doc_scores.values())
        if not scores:
            continue
        # Bottom-third cutoff inside the section; below that the doc is a
        # weak contributor to that report section's evidence.
        cutoff = scores[max(0, len(scores) // 3 - 1)]
        if section.doc_scores[doc_id] <= cutoff:
            label = _format_label_list(section.label_terms[:4])
            if label:
                weak_labels.append(label)
    if weak_labels:
        return [
            "다음 보고서 섹션에서는 이 자료의 근거가 약합니다: "
            + " · ".join(weak_labels[:2])
            + "."
        ]
    return []


def _consensus_notes(
    doc_id: str,  # noqa: ARG001 — kept for symmetry / future doc-level filtering
    consensus: ConsensusResult | None,
) -> list[str]:
    """At most one conflict line per doc — keep the card scannable."""
    if consensus is None or not consensus.conflicts:
        return []
    cluster_by_id = {cluster.id: cluster for cluster in consensus.concept_clusters}
    for flag in consensus.conflicts:
        cluster = cluster_by_id.get(flag.cluster_id)
        if cluster is None:
            continue
        label = _format_label_list(cluster.label_terms[:4])
        if not label:
            continue
        return [_conflict_message(label, flag)]
    return []


def _conflict_message(label: str, flag: ConflictFlag) -> str:
    if flag.type == "semantic_split":
        return f"'{label}'에 대해 자료들이 두 갈래로 갈리는 부분이 발견되었습니다."
    if flag.type == "cross_domain":
        return f"'{label}'에 대해 출처마다 입장이 다를 수 있어 교차 확인이 필요합니다."
    return f"'{label}'에 대해 출처 간 입장 차이가 있을 수 있습니다."


def _format_label_list(terms: list[str]) -> str:
    cleaned = [str(term).strip() for term in terms if str(term).strip()]
    return ", ".join(cleaned[:3])


# ---------------------------------------------------------------------------
# Detail breakdowns (drill-down for one document)
# ---------------------------------------------------------------------------


def section_breakdown_for(doc_id: str, sections: SectionResult | None) -> list[dict[str, Any]]:
    if sections is None:
        return []
    rows = [
        {
            "sectionId": section.id,
            "labels": list(section.label_terms[:6]),
            "score": round(float(section.doc_scores.get(doc_id, 0.0)), 6),
        }
        for section in sections.sections
    ]
    rows.sort(key=lambda item: -item["score"])
    return rows


def facet_breakdown_for(doc_id: str, intent: IntentResult | None) -> list[dict[str, Any]]:
    if intent is None or intent.doc_facet_matrix is None:
        return []
    try:
        column = intent.doc_order.index(doc_id)
    except ValueError:
        return []
    matrix = intent.doc_facet_matrix
    if column >= matrix.shape[1]:
        return []
    rows: list[dict[str, Any]] = []
    for row_idx, facet in enumerate(intent.facets):
        if row_idx >= matrix.shape[0]:
            break
        rows.append(
            {
                "facetId": facet.id,
                "labels": list(facet.label_terms[:6]),
                "score": round(float(matrix[row_idx, column]), 6),
            }
        )
    rows.sort(key=lambda item: -item["score"])
    return rows


def concept_participation_for(consensus: ConsensusResult | None) -> list[dict[str, Any]]:
    if consensus is None:
        return []
    rows = [
        {
            "clusterId": cluster.id,
            "labels": list(cluster.label_terms[:6]),
            "domainCount": len(cluster.domains),
            "diversity": round(float(cluster.diversity), 4),
            "composite": round(float(cluster.composite), 6),
            "hasConflict": any(
                flag.cluster_id == cluster.id for flag in consensus.conflicts
            ),
        }
        for cluster in consensus.concept_clusters
    ]
    rows.sort(key=lambda item: -item["composite"])
    return rows[:12]


__all__ = [
    "build_doc_items",
    "rank_normalize_percent",
    "level_for",
    "section_breakdown_for",
    "facet_breakdown_for",
    "concept_participation_for",
]
