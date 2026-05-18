"""Intent scoring math (VERIFY_DESIGN.md §4.3.2 / §4.2).

Pure-numpy helpers that turn fused chunk scores into the four output signals:

* ``query_doc_matrix``      — ``(M_query, N_doc)`` from per-query retrieval
* ``facet_doc_matrix``      — facet-grouped average over rows
* ``doc_intent_score``      — max / mean / breadth blend per doc
* ``coverage_gap``          — facets whose best doc still sits below threshold

No I/O, no retrieval calls — those live in ``intent_pipeline``. Keeping the
math here makes it trivial to unit-test (§1.4).
"""

from __future__ import annotations

import numpy as np

from ..indexing.bm25_index import BM25Index
from ..models import ChunkRecord, CoverageGap, Facet, VerificationConfig
from ..retrieval import aggregate_chunk_scores_to_docs, fused_chunk_scores_for_queries


def build_query_doc_matrix(
    query_texts: list[str],
    query_embeddings: np.ndarray,
    chunk_bm25: BM25Index,
    chunk_embeddings: np.ndarray,
    chunks: list[ChunkRecord],
    doc_order: list[str],
    cfg: VerificationConfig,
) -> np.ndarray:
    """Per-query, per-doc score matrix of shape ``(M_query, N_doc)``.

    Each query goes through the shared BM25 + dense + RRF retrieval, then
    chunk scores are aggregated into per-doc topK_mean scores. Docs not hit
    by a query stay at 0 — they are valid signals (low coverage), not gaps.
    ``out_size=None`` keeps every fused chunk so the doc aggregation sees
    every doc the query touches, not just the section-top.
    """
    matrix = np.zeros((len(query_texts), len(doc_order)), dtype=np.float32)
    if not query_texts or not doc_order:
        return matrix
    doc_index = {doc_id: position for position, doc_id in enumerate(doc_order)}

    for row, (text, embedding) in enumerate(zip(query_texts, query_embeddings)):
        fused = fused_chunk_scores_for_queries(
            [text],
            embedding[np.newaxis, :],
            chunk_bm25,
            chunk_embeddings,
            cfg,
            out_size=None,
        )
        doc_scores = aggregate_chunk_scores_to_docs(fused, chunks, cfg.doc_score_top_chunk)
        for doc_id, score in doc_scores.items():
            position = doc_index.get(doc_id)
            if position is not None:
                matrix[row, position] = score
    return matrix


def aggregate_to_facet_matrix(
    query_doc_matrix: np.ndarray,
    facet_groups: list[list[int]],
) -> np.ndarray:
    """Mean of each facet's query rows -> ``(N_facet, N_doc)``.

    Empty facets are skipped at the caller; here ``facet_groups`` already
    holds non-empty index lists in the row order callers will use.
    """
    if not facet_groups or query_doc_matrix.size == 0:
        return np.zeros((len(facet_groups), query_doc_matrix.shape[1]), dtype=np.float32)
    rows = [query_doc_matrix[group].mean(axis=0) for group in facet_groups]
    return np.vstack(rows).astype(np.float32)


def _softmax(matrix: np.ndarray, axis: int) -> np.ndarray:
    """Numerically stable softmax (subtract max before exp)."""
    shifted = matrix - matrix.max(axis=axis, keepdims=True)
    np.exp(shifted, out=shifted)
    shifted /= shifted.sum(axis=axis, keepdims=True) + 1e-12
    return shifted


def compute_doc_intent_score(
    facet_doc_matrix: np.ndarray,
    cfg: VerificationConfig,
) -> np.ndarray:
    """``(N_doc,)`` blend of per-doc max / mean / facet breadth (§4.3.2).

    * ``max``     — the doc's single strongest facet
    * ``mean``    — average coverage across facets
    * ``breadth`` — facet-axis entropy, normalized to [0, 1], so a doc spread
      evenly across facets scores higher than one peaking at a single facet

    The three weights live on :class:`VerificationConfig` so the blend can be
    re-tuned without code changes (§1.5).
    """
    n_facet, n_doc = facet_doc_matrix.shape
    if n_facet == 0 or n_doc == 0:
        return np.zeros(n_doc, dtype=np.float32)

    abs_max = facet_doc_matrix.max(axis=0)
    mean = facet_doc_matrix.mean(axis=0)

    if n_facet > 1:
        # x5 sharpens the softmax so a doc that genuinely peaks at one facet
        # is not lumped with one that is faintly spread across many.
        p = _softmax(facet_doc_matrix.astype(np.float64) * 5.0, axis=0)
        entropy = -(p * np.log(p + 1e-9)).sum(axis=0)
        breadth = entropy / np.log(n_facet)
    else:
        breadth = np.zeros(n_doc, dtype=np.float64)

    return (
        cfg.intent_weight_max * abs_max
        + cfg.intent_weight_mean * mean
        + cfg.intent_weight_breadth * breadth.astype(np.float32)
    ).astype(np.float32)


def detect_coverage_gaps(
    facets: list[Facet],
    facet_doc_matrix: np.ndarray,
    cfg: VerificationConfig,
) -> list[CoverageGap]:
    """Facets whose best doc still falls below ``intent_coverage_gap_threshold``.

    A gap means *no* corpus document covers the facet well enough; it is the
    intent-side analogue of Task 1's unmet-must_cover check.
    """
    if not facets or facet_doc_matrix.size == 0:
        return []
    threshold = cfg.intent_coverage_gap_threshold
    gaps: list[CoverageGap] = []
    for row, facet in enumerate(facets):
        top = float(facet_doc_matrix[row].max()) if facet_doc_matrix.shape[1] else 0.0
        if top < threshold:
            gaps.append(
                CoverageGap(
                    facet_id=facet.id,
                    label_terms=list(facet.label_terms),
                    top_doc_score=top,
                )
            )
    return gaps


__all__ = [
    "build_query_doc_matrix",
    "aggregate_to_facet_matrix",
    "compute_doc_intent_score",
    "detect_coverage_gaps",
]
