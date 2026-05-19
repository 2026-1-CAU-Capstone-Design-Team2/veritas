"""Per-section sentence retrieval — the deterministic half of new Task 1.

The LLM in :mod:`flow_planner` decides the report's narrative outline.
This module then asks, for every flow section, *which sentences across the
corpus best support that section?* — using the same dense+BM25+RRF pattern
the other tasks already share (no extra LLM calls).

Section "query text" = ``title + description + " ".join(keywords)``. We
embed that as a single dense vector and BM25-score it against every
sentence. Per-section RRF fuses the two channels and picks
``cfg.section_sentence_top_k`` sentences. Assignments are *exclusive*: each
sentence goes to the section whose RRF fused score is highest, so a single
strong sentence does not flood every section.
"""

from __future__ import annotations

import numpy as np

from ..indexing.bm25_index import BM25Index
from ..indexing.dense_index import DenseIndex
from ..indexing.rrf import reciprocal_rank_fusion
from ..models import (
    FlowSection,
    SentenceAssignment,
    SentenceUnit,
    VerificationConfig,
)


def _section_query_text(section: FlowSection) -> str:
    parts = [section.title]
    if section.description:
        parts.append(section.description)
    if section.keywords:
        parts.append(" ".join(section.keywords))
    return " ".join(p for p in parts if p).strip()


def _stack_sentence_embeddings(sentences: list[SentenceUnit]) -> np.ndarray:
    if not sentences:
        return np.zeros((0, 0), dtype=np.float32)
    dim = next((s.embedding.size for s in sentences if s.embedding is not None), 0)
    if dim == 0:
        return np.zeros((len(sentences), 0), dtype=np.float32)
    matrix = np.zeros((len(sentences), dim), dtype=np.float32)
    for i, sent in enumerate(sentences):
        if sent.embedding is not None and sent.embedding.size == dim:
            matrix[i] = sent.embedding.astype(np.float32, copy=False)
    return matrix


def _per_section_fused_scores(
    sections: list[FlowSection],
    section_queries: list[str],
    section_embeddings: np.ndarray,
    sentence_embeddings: np.ndarray,
    sentence_bm25: BM25Index,
    candidate_size: int,
    rrf_k: int,
) -> np.ndarray:
    """Return ``(N_section, N_sentence)`` RRF fused scores.

    Each row is one section's fused chunk-of-sentences scores. Sentences not
    in the candidate window for that section are scored 0 (RRF only ranks
    inside the candidate pool — anything outside is implicitly "not relevant
    enough", consistent with how the other tasks use RRF).
    """
    n_sections = len(sections)
    n_sentences = sentence_embeddings.shape[0]
    fused = np.zeros((n_sections, n_sentences), dtype=np.float64)
    if n_sections == 0 or n_sentences == 0:
        return fused

    for row, query_text in enumerate(section_queries):
        bm25_top = sentence_bm25.top_k(query_text, k=candidate_size)
        if sentence_embeddings.size and section_embeddings.size:
            cos = sentence_embeddings @ section_embeddings[row]
            dense_top = np.argsort(-cos, kind="stable")[:candidate_size].tolist()
            dense_top = [int(p) for p in dense_top]
        else:
            dense_top = []

        rankings: list[list[int]] = []
        if bm25_top:
            rankings.append(bm25_top)
        if dense_top:
            rankings.append(dense_top)
        if not rankings:
            continue

        for sent_idx, score in reciprocal_rank_fusion(
            rankings, k=rrf_k, out_size=None
        ):
            if 0 <= sent_idx < n_sentences:
                fused[row, sent_idx] = score
    return fused


def assign_sentences_to_sections(
    sentences: list[SentenceUnit],
    sentence_bm25: BM25Index,
    sections: list[FlowSection],
    dense: DenseIndex,
    cfg: VerificationConfig,
) -> list[FlowSection]:
    """Return ``sections`` with ``sentence_assignments`` filled in.

    The input ``sections`` is consumed in place — each FlowSection gets its
    ``sentence_assignments`` list populated and is returned. ``sentence_bm25``
    must already be built over ``[s.text for s in sentences]`` (facade owns
    index construction, §1.4). ``sentence`` embeddings are cached on each
    ``SentenceUnit`` so a re-run inside the same service instance never
    re-embeds.
    """
    if not sentences or not sections:
        return sections

    # Lazy-embed any sentence missing a cached embedding.
    missing = [(i, s) for i, s in enumerate(sentences) if s.embedding is None]
    if missing:
        new_embeds = dense.embed([s.text for _, s in missing])
        for (i, sent), vec in zip(missing, new_embeds):
            sent.embedding = vec
    sentence_embeddings = _stack_sentence_embeddings(sentences)

    section_queries = [_section_query_text(section) for section in sections]
    section_embeddings = dense.embed(section_queries)

    candidate_size = max(
        cfg.section_sentence_top_k,
        cfg.section_sentence_top_k * cfg.section_candidate_multiplier,
    )

    fused = _per_section_fused_scores(
        sections=sections,
        section_queries=section_queries,
        section_embeddings=section_embeddings,
        sentence_embeddings=sentence_embeddings,
        sentence_bm25=sentence_bm25,
        candidate_size=candidate_size,
        rrf_k=cfg.rrf_k,
    )

    # Exclusive assignment: each sentence to its single best-fitting section.
    # Sentences that no section ranks (fused row all zero) stay unassigned.
    if fused.size:
        best_section = fused.argmax(axis=0)
        best_score = fused.max(axis=0)
    else:
        best_section = np.zeros(0, dtype=int)
        best_score = np.zeros(0, dtype=np.float64)

    per_section_candidates: dict[int, list[tuple[int, float]]] = {
        section.id: [] for section in sections
    }
    for sent_idx, score in enumerate(best_score):
        if score <= 0.0:
            continue
        sec_id = sections[int(best_section[sent_idx])].id
        per_section_candidates[sec_id].append((sent_idx, float(score)))

    top_k = max(1, int(cfg.section_sentence_top_k))
    for section in sections:
        candidates = per_section_candidates.get(section.id, [])
        candidates.sort(key=lambda kv: (-kv[1], kv[0]))
        section.sentence_assignments = [
            SentenceAssignment(
                doc_id=sentences[idx].doc_id,
                paragraph_index=sentences[idx].paragraph_index,
                sentence_index=sentences[idx].sentence_index,
                text=sentences[idx].text,
                fit_score=score,
            )
            for idx, score in candidates[:top_k]
        ]
    return sections


__all__ = ["assign_sentences_to_sections"]
