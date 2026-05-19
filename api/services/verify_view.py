"""Adapt the verification domain model to the frontend's UI schema.

Kept separate from ``verify_service.py`` so the *thin* API layer stays thin:
service.py owns the request flow (workspace resolution, persistence load,
progress polling), this module owns the *translation* into the user-facing
shape (``docId / title / level / rationale / issues`` + detail breakdowns).

All non-technical phrasing — level labels, issue sentences — lives here. The
algorithm layer (``services/verification/``) never sees Korean labels.

History (kept here so future readers do not rediscover the same mistake):
the previous version of this module mapped a BM25+Dense+RRF "의도 일치율"
into a percentage band. The raw scores were tiny (≲ 0.02) and the rendered
percentage was a workspace-internal ratio that users could not interpret.
The reliability task replaces that signal entirely; the LLM emits the band
directly and the rationale that justified it.
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
from services.verification.reliability import (
    ReliabilityItem,
    ReliabilityResult,
)

# Map LLM verdict ("high" / "medium" / "low") -> Korean badge label so the
# rest of the UI (filter chips, badges) keeps using the same vocabulary
# without knowing about the underlying English token.
_LEVEL_LABEL = {
    "high": "높음",
    "medium": "중간",
    "low": "낮음",
}
_LEVEL_LABEL_REVERSE = {label: key for key, label in _LEVEL_LABEL.items()}
# Sort order so "높음" docs rise to the top of the verify list.
_LEVEL_SORT_RANK = {"high": 0, "medium": 1, "low": 2}

# Human-readable signal labels — surfaced on the detail dialog.
_SIGNAL_LABEL = {
    "authority": "출처 권위",
    "verifiability": "검증 가능성",
    "self_consistency": "자기일관성",
}
_SIGNAL_STRENGTH_LABEL = {
    "strong": "강함",
    "mixed": "보통",
    "weak": "약함",
}

_MENTION_KIND_LABEL = {
    "new_finding": "New Finding",
    "reliability_note": "Reliability Note",
}

# Cap how much batch evidence we hand the UI per doc so a verbose run does
# not flood a card. The detail dialog can show more if we ever expose a
# drill-down for it.
_BATCH_MENTIONS_PER_CARD = 3


# ---------------------------------------------------------------------------
# Per-doc summary items (list view)
# ---------------------------------------------------------------------------


def build_doc_items(
    artifacts: VerificationArtifacts,
    doc_titles: dict[str, str],
) -> list[dict[str, Any]]:
    """One ranked item per doc — what the verify card list renders.

    Driven by ``artifacts.reliability`` (the LLM-graded verdict). Older
    workspaces that only carry a legacy ``intent_coverage.json`` end up with
    a neutral "중간" verdict for every doc — they need a fresh verify run to
    get the LLM verdict.
    """
    reliability: ReliabilityResult | None = artifacts.reliability
    sections: SectionResult | None = artifacts.sections
    consensus: ConsensusResult | None = artifacts.consensus

    items_by_doc: dict[str, ReliabilityItem] = {}
    if reliability is not None:
        items_by_doc = {item.doc_id: item for item in reliability.items}

    # Duplicate documents inherit their verdict from their source (so the
    # reliability artifact stays complete on disk) but the verify card list
    # must NOT show them as separate cards — they are not standalone sources.
    # ``_load_doc_titles`` already skips ``duplicate_of`` records, so taking
    # its key set as the canonical doc list automatically drops dup_NNN ids
    # that crept into ``reliability.items`` via the inheritance pass.
    doc_ids = sorted(doc_titles.keys())

    items: list[dict[str, Any]] = []
    for doc_id in doc_ids:
        verdict = items_by_doc.get(doc_id)
        if verdict is None:
            level_key = "medium"
            rationale = "검증을 다시 실행하면 신뢰도 등급이 갱신됩니다."
            signals_payload: list[dict[str, str]] = []
            mentions_payload: list[dict[str, str]] = []
            inherited_from = None
        else:
            level_key = verdict.level if verdict.level in _LEVEL_LABEL else "medium"
            rationale = verdict.rationale
            signals_payload = _format_signals(verdict.signals)
            mentions_payload = _format_mentions(
                verdict.batch_mentions, limit=_BATCH_MENTIONS_PER_CARD
            )
            inherited_from = verdict.inherited_from

        level_label = _LEVEL_LABEL[level_key]
        items.append(
            {
                "docId": doc_id,
                "title": doc_titles.get(doc_id, f"문서 {doc_id}"),
                "level": level_label,
                "levelKey": level_key,
                # Headline phrase the card shows under the title — replaces
                # the old "의도 일치율 N%" string.
                "matchRate": f"신뢰도 {level_label}",
                "reliabilityRationale": rationale,
                "reliabilitySignals": signals_payload,
                "batchMentions": mentions_payload,
                "inheritedFrom": inherited_from,
                "issues": _issues_for_doc(
                    doc_id=doc_id,
                    rationale=rationale,
                    mentions=mentions_payload,
                    sections=sections,
                    consensus=consensus,
                    inherited_from=inherited_from,
                ),
            }
        )

    items.sort(
        key=lambda item: (
            _LEVEL_SORT_RANK.get(item["levelKey"], 9),
            item["docId"],
        )
    )
    return items


def _format_signals(signals: dict[str, str]) -> list[dict[str, str]]:
    """Render the three sub-signals into a UI-friendly ordered list.

    Returned order matches the prompt's order (authority → verifiability →
    self_consistency) so the detail dialog reads top-down the same way the
    LLM was asked to judge.
    """
    rendered: list[dict[str, str]] = []
    for key in ("authority", "verifiability", "self_consistency"):
        strength = (signals or {}).get(key, "mixed")
        rendered.append(
            {
                "key": key,
                "label": _SIGNAL_LABEL.get(key, key),
                "strength": strength,
                "strengthLabel": _SIGNAL_STRENGTH_LABEL.get(strength, strength),
            }
        )
    return rendered


def _format_mentions(mentions: list[Any], *, limit: int) -> list[dict[str, str]]:
    rendered: list[dict[str, str]] = []
    for mention in mentions[:limit]:
        batch_id = getattr(mention, "batch_id", "")
        kind = getattr(mention, "kind", "")
        snippet = getattr(mention, "snippet", "")
        rendered.append(
            {
                "batchId": str(batch_id),
                "kind": str(kind),
                "kindLabel": _MENTION_KIND_LABEL.get(str(kind), str(kind)),
                "snippet": str(snippet),
            }
        )
    return rendered


def level_label(level_key: str) -> str:
    """Stable mapping from internal key to display label.

    Kept as a public helper so other modules (notably the get_summary
    distribution counts) can render the same band names without re-deriving
    them. The reverse mapping ``_LEVEL_LABEL_REVERSE`` covers the legacy
    code paths that still hand a Korean string and need the English key.
    """
    return _LEVEL_LABEL.get(level_key, "중간")


# ---------------------------------------------------------------------------
# Issues (short plain-Korean lines)
# ---------------------------------------------------------------------------


def _issues_for_doc(
    *,
    doc_id: str,
    rationale: str,
    mentions: list[dict[str, str]],
    sections: SectionResult | None,
    consensus: ConsensusResult | None,
    inherited_from: str | None,
) -> list[str]:
    """At most a handful of one-line notes about this document.

    Order matters: the card footer only renders the *first* line, so the
    most informative signal goes first. Priority:
      1. duplicate inheritance — most important context if present
      2. LLM rationale (the one-line trust summary)
      3. concrete batch-mention snippet (what *this* doc contributed)
      4. supporting sections (where the writer can use this doc)
      5. cross-source conflict touching this concept (if any)
    """
    notes: list[str] = []
    if inherited_from:
        notes.append(
            f"원본 자료(doc_{inherited_from})와 동일 컨텐츠로 식별되어 등급을 상속받았습니다."
        )
    if rationale:
        notes.append(rationale)
    notes.extend(_mention_notes(mentions))
    notes.extend(_section_notes(doc_id, sections))
    notes.extend(_consensus_notes(doc_id, consensus))
    return notes


def _mention_notes(mentions: list[dict[str, str]]) -> list[str]:
    if not mentions:
        return []
    first = mentions[0]
    kind = str(first.get("kindLabel") or "Batch")
    snippet = str(first.get("snippet") or "").strip()
    if not snippet:
        return []
    return [f"이 자료의 기여({kind}): {snippet}"]


def _section_notes(
    doc_id: str,
    sections: SectionResult | None,
) -> list[str]:
    """Tell the user which flow sections actually use sentences from this doc."""
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

    Carries the per-section ``topDocs`` (the documents that contributed the
    most sentence weight) so the writer can grab their text first; the
    sentence assignments themselves are inlined for the detail dialog so it
    does not need a second round-trip.
    """
    sections = artifacts.sections
    if sections is None or not sections.sections:
        return []
    ordered = sorted(sections.sections, key=lambda s: (s.order, s.id))
    rows: list[dict[str, Any]] = []
    for section in ordered:
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

    * ``underweighted_section`` — a flow-planned section the corpus cannot
      really support (fewer than ~1/3 of ``section_sentence_top_k``).
    * ``low_reliability`` — a doc the LLM judged low-trust.
    * ``conflict`` — a concept cluster where sources disagree.

    Ordered so the most actionable items appear first.
    """
    rows: list[dict[str, Any]] = []

    _UNDERWEIGHTED_MIN_SENTENCES = 8
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

    # Low-reliability per-doc issues — surface the LLM's rationale so the
    # writer immediately sees why it landed in 낮음.
    if artifacts.reliability is not None:
        for item in artifacts.reliability.items:
            if item.level != "low":
                continue
            doc_title = doc_titles.get(item.doc_id, f"문서 {item.doc_id}")
            rationale = (item.rationale or "").strip()
            detail = doc_title
            if rationale and rationale not in detail:
                detail = f"{doc_title} — {rationale}"
            rows.append(
                {
                    "kind": "low_reliability",
                    "title": "출처 신뢰도가 낮게 평가된 자료",
                    "detail": detail,
                    "hint": "동일 주제의 권위 있는 다른 자료로 교차 검증한 뒤 인용하세요.",
                    "metric": f"doc_{item.doc_id}",
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

    order = {
        "underweighted_section": 0,
        "low_reliability": 1,
        "conflict": 2,
    }
    rows.sort(key=lambda row: order.get(row["kind"], 99))

    _ = doc_titles
    return rows


# ---------------------------------------------------------------------------
# Legacy aliases — kept so existing imports do not break while old workspaces
# are still being read.
# ---------------------------------------------------------------------------


def facet_breakdown_for(doc_id: str, intent: IntentResult | None) -> list[dict[str, Any]]:
    """Legacy facet breakdown — only meaningful when the legacy intent task ran.

    Newly-verified workspaces have ``intent is None`` and this returns ``[]``,
    which the UI renders as an empty panel.
    """
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


def ratio_normalize_percent(scores: dict[str, float]) -> dict[str, int]:
    """Legacy helper — no longer used by the verify card pipeline.

    Kept so any external smoke test that imported the old name still
    resolves. Returns an empty dict for an empty input rather than raising.
    """
    if not scores:
        return {}
    max_score = max(scores.values())
    if max_score <= 0.0:
        return {doc: 0 for doc in scores}
    return {
        doc: max(0, min(100, int(round(score / max_score * 100))))
        for doc, score in scores.items()
    }


# Backwards-compatible aliases for any caller that imported the old names.
rank_normalize_percent = ratio_normalize_percent


def level_for(percent: int) -> str:
    """Legacy converter — clamps a percent into a Korean band label.

    The current verify pipeline does not use this; LLM verdicts carry the
    band directly. The function stays so any legacy code path (or test)
    that called it still resolves.
    """
    if percent >= 70:
        return "높음"
    if percent >= 40:
        return "중간"
    return "낮음"


__all__ = [
    "build_doc_items",
    "level_label",
    "section_breakdown_for",
    "concept_participation_for",
    "sections_overview",
    "issues_overview",
    # Legacy
    "facet_breakdown_for",
    "ratio_normalize_percent",
    "rank_normalize_percent",
    "level_for",
]
