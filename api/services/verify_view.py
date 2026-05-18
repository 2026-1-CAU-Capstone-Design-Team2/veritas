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
    SentenceAssignment,
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
    rank_pct = ratio_normalize_percent(intent_scores)
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


def ratio_normalize_percent(scores: dict[str, float]) -> dict[str, int]:
    """Workspace-internal proportion of the best raw score, 0-100.

    The doc with the highest raw intent score lands at 100; every other doc
    scales linearly as ``raw / max * 100``. Unlike a rank percentile, this
    preserves the raw score *distribution* — a workspace where docs cluster
    tightly reads as a tight band near the top (and a meaningful average),
    while a workspace with a clear standout shows it as a wide spread.

    Why not the raw RRF score itself: RRF scores are tiny (≲ 0.02), so
    showing them as a "match rate" reads as 1% to a user. Scaling against the
    workspace's own max keeps the relative order *and* makes the unit
    intuitive ("% of the best doc in this workspace").
    """
    if not scores:
        return {}
    max_score = max(scores.values())
    if max_score <= 0.0:
        # Every doc has zero intent signal — surface them all as 0% rather
        # than dividing by zero. The caller will mark them "낮음".
        return {doc: 0 for doc in scores}
    return {
        doc: max(0, min(100, int(round(score / max_score * 100))))
        for doc, score in scores.items()
    }


# Backwards-compatible alias for any caller that imported the old name.
# The rank-percentile semantics it used to carry (forced uniform distribution,
# constant ~50% workspace average) hid genuine quality differences between
# workspaces, so the implementation now delegates to the ratio normalizer.
rank_normalize_percent = ratio_normalize_percent


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
    """Tell the user which flow sections actually use sentences from this doc.

    Rewritten for the sentence-flow model: a doc "contributes" to a section
    when at least one of its sentences was assigned there. Surfacing the
    contribution makes the per-doc card immediately useful to the writer
    ("이 자료는 *서론* / *본론* 의 어디에 쓸 수 있다").
    """
    if sections is None or not sections.sections:
        return []

    contributed: list[str] = []
    for section in sections.sections:
        if any(a.doc_id == doc_id for a in section.sentence_assignments):
            title = (section.title or f"섹션 {section.id}").strip()
            if title:
                contributed.append(title)
    if not contributed:
        return ["이 자료의 문장은 어느 보고서 섹션에도 할당되지 않았습니다."]
    return [
        "이 자료가 받쳐주는 섹션: " + " · ".join(contributed[:3]) + "."
    ]


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
    """For one doc, which flow sections include sentences from it, and how many.

    Score = the sum of ``fit_score`` of this doc's sentences in each section.
    Sections with zero contribution are still listed (with score 0) so the
    user sees a complete map of the report's flow even when this doc only
    helps with part of it.
    """
    if sections is None:
        return []
    rows: list[dict[str, Any]] = []
    for section in sections.sections:
        contributions = [
            a for a in section.sentence_assignments if a.doc_id == doc_id
        ]
        total = sum(float(a.fit_score) for a in contributions)
        rows.append(
            {
                "sectionId": section.id,
                "labels": [section.title] + list(section.keywords[:5]),
                "score": round(total, 6),
                "sentenceCount": len(contributions),
            }
        )
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


def sections_overview(artifacts: VerificationArtifacts) -> list[dict[str, Any]]:
    """One row per *ordered* flow section — what the section panel renders.

    The new sentence-flow model carries:
      * an ``order`` (writer-readable left-to-right narrative)
      * a ``role`` (intro / body / conclusion) the UI chips up
      * a list of ``SentenceAssignment`` per section

    Per-section ``topDocs`` are the documents that contributed the most
    sentence weight to *this* section (i.e. the writer can grab their text
    first). ``sentenceCount`` and ``documentCount`` are the section's
    quick-glance scope.
    """
    sections = artifacts.sections
    if sections is None or not sections.sections:
        return []
    ordered = sorted(sections.sections, key=lambda s: (s.order, s.id))
    rows: list[dict[str, Any]] = []
    for section in ordered:
        # Aggregate per-doc weight inside this section.
        doc_weight: dict[str, float] = {}
        for assignment in section.sentence_assignments:
            doc_weight[assignment.doc_id] = (
                doc_weight.get(assignment.doc_id, 0.0) + float(assignment.fit_score)
            )
        top_docs = [
            doc_id
            for doc_id, _ in sorted(doc_weight.items(), key=lambda kv: -kv[1])[:5]
        ]
        rows.append(
            {
                "sectionId": section.id,
                "order": section.order,
                "title": section.title,
                "description": section.description,
                "role": section.role,
                "labels": list(section.keywords[:8]),
                "sentenceCount": len(section.sentence_assignments),
                "documentCount": len(doc_weight),
                "topDocs": top_docs,
                "sentenceAssignments": [
                    {
                        "docId": a.doc_id,
                        "paragraphIndex": a.paragraph_index,
                        "sentenceIndex": a.sentence_index,
                        "text": a.text,
                        "fitScore": round(float(a.fit_score), 6),
                    }
                    for a in section.sentence_assignments
                ],
            }
        )
    return rows


def issues_overview(
    artifacts: VerificationArtifacts,
    doc_titles: dict[str, str],
) -> list[dict[str, Any]]:
    """Flatten every detected issue into one user-readable list.

    Three kinds, each with a short non-technical message:

    * ``unmet_must_cover`` — a topic the plan wanted covered but the corpus
      does not actually support.
    * ``intent_gap``       — a user-intent facet no document covers well.
    * ``conflict``         — a concept cluster where sources disagree.

    Ordered so the most actionable items (unmet must_cover, then conflicts,
    then gaps) appear first.
    """
    rows: list[dict[str, Any]] = []

    # New "근거 부족 섹션" signal: an LLM-planned section the corpus cannot
    # actually support (too few sentences assigned to it). Threshold is small
    # by design — we only want to flag the truly underweighted sections.
    _UNDERWEIGHTED_MIN_SENTENCES = 3
    if artifacts.sections is not None:
        for section in artifacts.sections.sections:
            if len(section.sentence_assignments) >= _UNDERWEIGHTED_MIN_SENTENCES:
                continue
            title = (section.title or f"섹션 {section.id}").strip()
            rows.append(
                {
                    "kind": "underweighted_section",
                    "title": "보고서 흐름에 필요하지만 자료에서 충분히 받쳐주지 못한 섹션",
                    "detail": title,
                    "hint": "이 섹션을 다룬 자료를 추가로 확보하거나, 흐름을 재설계할 수 있습니다.",
                    "metric": f"배치된 문장 {len(section.sentence_assignments)}개",
                }
            )

    if artifacts.consensus is not None:
        cluster_by_id = {
            cluster.id: cluster
            for cluster in artifacts.consensus.concept_clusters
        }
        for flag in artifacts.consensus.conflicts:
            cluster = cluster_by_id.get(flag.cluster_id)
            label = _format_label_list(cluster.label_terms[:4]) if cluster else ""
            if flag.type == "semantic_split":
                title = "한 개념이 자료들에서 두 갈래로 갈리는 부분"
                hint = "두 해석을 모두 확인하고, 어떤 입장을 채택할지 결정하세요."
            elif flag.type == "cross_domain":
                title = "출처마다 입장이 다를 수 있는 주제"
                hint = "다양한 출처를 비교해 교차 검증한 뒤 인용하세요."
            else:
                title = "출처 간 입장 차이가 있을 수 있는 주제"
                hint = "추가 확인이 필요합니다."
            rows.append(
                {
                    "kind": "conflict",
                    "title": title,
                    "detail": label or "(개념 라벨이 비어 있는 클러스터)",
                    "hint": hint,
                    "metric": f"차이 점수 {flag.score:.3f}",
                }
            )

    if artifacts.intent is not None:
        for gap in artifacts.intent.coverage_gap:
            label = _format_label_list(gap.label_terms[:4]) or "(라벨이 비어 있는 의도)"
            rows.append(
                {
                    "kind": "intent_gap",
                    "title": "사용자 의도 중 자료가 충분히 받쳐주지 못한 주제",
                    "detail": label,
                    "hint": "이 주제를 잘 다룬 자료가 거의 없어 보강이 필요합니다.",
                    "metric": f"최고 자료 점수 {gap.top_doc_score:.3f}",
                }
            )

    order = {"underweighted_section": 0, "conflict": 1, "intent_gap": 2}
    rows.sort(key=lambda row: order.get(row["kind"], 99))

    # ``doc_titles`` is accepted for future per-doc surfacing; the three issue
    # kinds are workspace-level today, not individual docs.
    _ = doc_titles
    return rows


__all__ = [
    "build_doc_items",
    "ratio_normalize_percent",
    "rank_normalize_percent",  # alias — kept for the existing smoke test
    "level_for",
    "section_breakdown_for",
    "facet_breakdown_for",
    "concept_participation_for",
    "sections_overview",
    "issues_overview",
]
