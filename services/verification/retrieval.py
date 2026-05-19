"""Multi-query chunk retrieval shared by Task 1 (sections) and Task 2 (intent).

VERIFY_DESIGN.md §4.5 (DRY): "Task 1과 Task 2는 query만 다르고 retrieval 절차가
같음 — 공통 함수를 만들어 양쪽에서 호출하라." This module is that common layer.
It only orchestrates the index primitives in ``indexing/`` — it owns no state
and no task semantics.

The pipeline shape both tasks share, given one or more queries:

1. each query produces a BM25 ranking *and* a dense (cosine) ranking over
   the chunk corpus
2. the rankings are RRF-fused into one chunk ordering (rank fusion sidesteps
   incommensurable BM25 vs cosine scales — §2.4)
3. the fused chunk ranking is aggregated to doc-level scores via topK_mean
   (§3.4), so a single high-scoring chunk does not let a doc dominate
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

from .indexing.bm25_index import BM25Index
from .indexing.rrf import reciprocal_rank_fusion
from .models import ChunkRecord, VerificationConfig


def stack_chunk_embeddings(chunks: Sequence[ChunkRecord]) -> np.ndarray:
    """Stack per-chunk embedding vectors into one ``(N_chunk, d)`` matrix.

    ``ArtifactLoader`` already L2-normalizes each vector, so cosine similarity
    against this matrix is a plain dot product. Chunks whose embedding is
    missing get a zero row, preserving positional alignment with ``chunks``.
    """
    if not chunks:
        return np.zeros((0, 0), dtype=np.float32)
    dim = next((c.embedding.size for c in chunks if c.embedding is not None), 0)
    if dim == 0:
        return np.zeros((len(chunks), 0), dtype=np.float32)
    matrix = np.zeros((len(chunks), dim), dtype=np.float32)
    for position, chunk in enumerate(chunks):
        if chunk.embedding is not None and chunk.embedding.size == dim:
            matrix[position] = chunk.embedding.astype(np.float32, copy=False)
    return matrix


def chunk_rankings_for_query(
    query_text: str,
    query_embedding: np.ndarray,
    chunk_bm25: BM25Index,
    chunk_embeddings: np.ndarray,
    candidate_size: int,
) -> tuple[list[int], list[int]]:
    """BM25-top and dense-top chunk positions for a single query.

    Returns ``(bm25_ranking, dense_ranking)`` — each a list of chunk
    positions, best first, truncated to ``candidate_size``. Dense uses a
    stable argsort so ties keep corpus order, matching :class:`BM25Index`.
    """
    bm25_ranking = chunk_bm25.top_k(query_text, k=candidate_size)

    if chunk_embeddings.size == 0:
        dense_ranking: list[int] = []
    else:
        cosine = chunk_embeddings @ np.asarray(query_embedding, dtype=np.float32)
        dense_ranking = np.argsort(-cosine, kind="stable")[:candidate_size].tolist()
        dense_ranking = [int(p) for p in dense_ranking]

    return bm25_ranking, dense_ranking


def fused_chunk_scores_for_queries(
    query_texts: Sequence[str],
    query_embeddings: np.ndarray,
    chunk_bm25: BM25Index,
    chunk_embeddings: np.ndarray,
    cfg: VerificationConfig,
    *,
    out_size: int | None = None,
) -> list[tuple[int, float]]:
    """RRF-fuse the BM25 + dense rankings of every query into one chunk list.

    Each query contributes two rankings, both candidate-truncated to
    ``cfg.section_top_chunk * cfg.section_candidate_multiplier``. ``out_size``
    overrides the default truncation (``cfg.section_top_chunk``) when a caller
    needs more (Task 2 keeps a richer ranking for doc aggregation).
    """
    if len(query_texts) == 0:
        return []

    candidate_size = max(1, cfg.section_top_chunk * cfg.section_candidate_multiplier)
    rankings: list[list[int]] = []
    for text, embedding in zip(query_texts, query_embeddings):
        bm25_ranking, dense_ranking = chunk_rankings_for_query(
            text, embedding, chunk_bm25, chunk_embeddings, candidate_size
        )
        if bm25_ranking:
            rankings.append(bm25_ranking)
        if dense_ranking:
            rankings.append(dense_ranking)

    if not rankings:
        return []

    return reciprocal_rank_fusion(
        rankings,
        k=cfg.rrf_k,
        out_size=out_size if out_size is not None else cfg.section_top_chunk,
    )


def aggregate_chunk_scores_to_docs(
    chunk_scores: Sequence[tuple[int, float]],
    chunk_records: Sequence[ChunkRecord],
    top_k: int,
) -> dict[str, float]:
    """Aggregate fused chunk scores into per-doc scores via topK_mean (§3.4).

    A doc's score is the mean of its top ``top_k`` chunk scores; one very
    strong chunk cannot inflate a long doc. Positions out of range (e.g. from
    stale rankings) are silently skipped.
    """
    by_doc: dict[str, list[float]] = {}
    for position, score in chunk_scores:
        if 0 <= position < len(chunk_records):
            by_doc.setdefault(chunk_records[position].parent_doc_id, []).append(float(score))
    return {
        doc_id: float(np.mean(sorted(scores, reverse=True)[: max(1, top_k)]))
        for doc_id, scores in by_doc.items()
    }


__all__ = [
    "stack_chunk_embeddings",
    "chunk_rankings_for_query",
    "fused_chunk_scores_for_queries",
    "aggregate_chunk_scores_to_docs",
]
