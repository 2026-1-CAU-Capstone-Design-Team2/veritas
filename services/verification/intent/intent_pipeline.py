"""Task 2 entry point — assemble intent coverage (VERIFY_DESIGN.md §4).

This module only *orchestrates*: facet extraction, community detection,
retrieval and labelling live in their own modules (§1.1). The retrieval call
goes through ``services/verification/retrieval.py`` — same code path as Task 1
(§4.5).
"""

from __future__ import annotations

import logging

import numpy as np

from ..graph import build_cosine_graph, detect_communities
from ..indexing.bm25_index import BM25Index
from ..indexing.dense_index import DenseIndex
from ..labeling import label_groups
from ..models import (
    ChunkRecord,
    DocRecord,
    Facet,
    IntentResult,
    VerificationConfig,
)
from ..retrieval import stack_chunk_embeddings
from ..tokenization import HybridTokenizer
from .facet_extraction import extract_intent_queries
from .scoring import (
    aggregate_to_facet_matrix,
    build_query_doc_matrix,
    compute_doc_intent_score,
    detect_coverage_gaps,
)

logger = logging.getLogger(__name__)


def run_intent_pipeline(
    docs: list[DocRecord],
    chunks: list[ChunkRecord],
    chunk_bm25: BM25Index,
    dense: DenseIndex,
    request_text: str,
    plan: dict,
    grounding: dict,
    cfg: VerificationConfig,
    *,
    chunk_embeddings: np.ndarray | None = None,
    tokenizer: HybridTokenizer | None = None,
) -> IntentResult:
    """Decompose intent into facets and score every doc on facet coverage.

    ``docs`` defines ``doc_order``: the column axis of every per-doc matrix in
    the returned :class:`IntentResult`. ``chunk_bm25`` is expected to be
    pre-built over chunk texts (facade owns indexing, §1.4). ``chunk_embeddings``
    is the matrix the facade caches between Task 1 and Task 2; rebuilt on demand
    if ``None``.
    """
    queries = extract_intent_queries(request_text, plan, grounding)
    if not queries or not docs or not chunks:
        if not queries:
            logger.warning("verification: no intent queries extracted from artifacts")
        return IntentResult(doc_order=[doc.doc_id for doc in docs])

    tokenizer = tokenizer or HybridTokenizer()
    doc_order = [doc.doc_id for doc in docs]

    # 1. Embed every intent query — cache vectors on the records so callers can
    #    inspect them later without re-embedding.
    query_texts = [q.text for q in queries]
    query_embeddings = dense.embed(query_texts)
    for query, vector in zip(queries, query_embeddings):
        query.embedding = vector

    # 2. Community-detect intent queries into facets (same algorithm as §3.3.1).
    query_graph = build_cosine_graph(query_embeddings, cfg.intent_query_edge_threshold)
    communities = detect_communities(query_graph, cfg.community_resolution, seed=cfg.random_seed)
    facet_groups = [group for group in communities if group]

    if not facet_groups:
        return IntentResult(doc_order=doc_order)

    # 3. Per-query doc score matrix — one row per query, one column per doc.
    if chunk_embeddings is None:
        chunk_embeddings = stack_chunk_embeddings(chunks)
    query_doc_matrix = build_query_doc_matrix(
        query_texts,
        query_embeddings,
        chunk_bm25,
        chunk_embeddings,
        chunks,
        doc_order,
        cfg,
    )

    # 4. Facet-level aggregation (mean over each facet's query rows).
    facet_doc_matrix = aggregate_to_facet_matrix(query_doc_matrix, facet_groups)

    # 5. c-TF-IDF labels per facet, drawn from the queries' own text only.
    #    Pulling chunk text in would label every facet with whichever doc
    #    dominates retrieval — the facet's own queries are the cleaner signal.
    facet_corpora = {
        facet_id: "\n".join(queries[i].text for i in group)
        for facet_id, group in enumerate(facet_groups)
    }
    facet_labels = label_groups(facet_corpora, tokenizer, cfg)

    facets = [
        Facet(
            id=facet_id,
            label_terms=facet_labels.get(facet_id, []),
            origin_queries=[queries[i].origin for i in group],
        )
        for facet_id, group in enumerate(facet_groups)
    ]

    # 6. Doc-level intent score + facet-level coverage gaps.
    intent_scores = compute_doc_intent_score(facet_doc_matrix, cfg)
    doc_intent_score = {doc_id: float(intent_scores[col]) for col, doc_id in enumerate(doc_order)}
    coverage_gap = detect_coverage_gaps(facets, facet_doc_matrix, cfg)

    return IntentResult(
        facets=facets,
        doc_facet_matrix=facet_doc_matrix,
        doc_intent_score=doc_intent_score,
        coverage_gap=coverage_gap,
        doc_order=doc_order,
    )


__all__ = ["run_intent_pipeline"]
