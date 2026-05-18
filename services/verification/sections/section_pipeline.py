"""Task 1 entry point — assemble section clustering (VERIFY_DESIGN.md §3).

This module only *orchestrates*: every algorithm body lives in a shared
module (``graph``, ``labeling``, ``retrieval``). §1.1.

Pipeline: embed ``plan.must_cover[]`` -> cosine community detection
-> per-section BM25 + dense retrieval, RRF-fused -> doc-level topK_mean
aggregation -> c-TF-IDF labelling -> unmet-must_cover self-check (§3.5).
"""

from __future__ import annotations

import logging

import numpy as np

from ..graph import build_cosine_graph, detect_communities
from ..indexing.bm25_index import BM25Index
from ..indexing.dense_index import DenseIndex
from ..labeling import label_groups
from ..models import (
    ChunkEvidence,
    ChunkRecord,
    Section,
    SectionResult,
    UnmetMustCover,
    VerificationConfig,
)
from ..retrieval import (
    aggregate_chunk_scores_to_docs,
    fused_chunk_scores_for_queries,
    stack_chunk_embeddings,
)
from ..tokenization import HybridTokenizer

logger = logging.getLogger(__name__)


def _clean_must_cover(plan: dict) -> list[str]:
    """Drop empty / whitespace ``must_cover`` entries while preserving order.

    ``plan.json`` may carry blanks (LLM noise); keeping them would skew
    community detection and produce empty sections.
    """
    items = plan.get("must_cover") or []
    return [str(text).strip() for text in items if isinstance(text, str) and text.strip()]


def run_section_pipeline(
    chunks: list[ChunkRecord],
    chunk_bm25: BM25Index,
    dense: DenseIndex,
    plan: dict,
    cfg: VerificationConfig,
    *,
    chunk_embeddings: np.ndarray | None = None,
    tokenizer: HybridTokenizer | None = None,
) -> SectionResult:
    """Cluster ``must_cover`` items into sections and retrieve evidence per section.

    ``chunk_bm25`` is expected to be already built over ``[c.text for c in
    chunks]`` (the facade owns index construction, §1.4). ``chunk_embeddings``
    is the ``(N_chunk, d)`` matrix the facade can cache once and pass to both
    Task 1 and Task 2; if ``None``, it is rebuilt from ``chunks``.
    """
    must_cover = _clean_must_cover(plan)
    if not must_cover or not chunks:
        if not must_cover:
            logger.warning("verification: plan.must_cover is empty — no sections to cluster")
        return SectionResult()

    tokenizer = tokenizer or HybridTokenizer()

    # 1. must_cover query embeddings and community partition.
    cover_embeddings = dense.embed(must_cover)
    query_graph = build_cosine_graph(cover_embeddings, cfg.section_query_edge_threshold)
    communities = detect_communities(query_graph, cfg.community_resolution, seed=cfg.random_seed)

    # 2. Chunk embedding matrix — reused for cosine + dense ranking.
    if chunk_embeddings is None:
        chunk_embeddings = stack_chunk_embeddings(chunks)

    # 3. Per-section retrieval -> chunk evidence + doc-level scores.
    sections: list[Section] = []
    section_corpora: dict[int, str] = {}
    for section_id, group in enumerate(communities):
        if not group:
            continue
        query_texts = [must_cover[i] for i in group]
        query_embeddings = cover_embeddings[group]

        fused = fused_chunk_scores_for_queries(
            query_texts,
            query_embeddings,
            chunk_bm25,
            chunk_embeddings,
            cfg,
            out_size=cfg.section_top_chunk,
        )

        chunk_evidence = [
            ChunkEvidence(
                doc_id=chunks[position].parent_doc_id,
                chunk_id=chunks[position].chunk_id,
                rrf_score=float(score),
            )
            for position, score in fused
            if 0 <= position < len(chunks)
        ]
        doc_scores = aggregate_chunk_scores_to_docs(fused, chunks, cfg.doc_score_top_chunk)

        sections.append(
            Section(
                id=section_id,
                origin_must_cover_indices=list(group),
                label_terms=[],
                chunk_evidence=chunk_evidence,
                doc_scores=doc_scores,
            )
        )

        # c-TF-IDF pseudo-doc: this section's queries + the texts of its top
        # chunks. The queries anchor the label terms; the chunks broaden the
        # vocabulary so labels are not just literal must_cover phrases.
        section_corpora[section_id] = "\n".join(
            query_texts + [chunks[position].text for position, _ in fused if 0 <= position < len(chunks)]
        )

    # 4. Auto-label sections from their own pseudo-doc vocabulary.
    labels = label_groups(section_corpora, tokenizer, cfg)
    for section in sections:
        section.label_terms = labels.get(section.id, [])

    # 5. Unmet-must_cover self-check (§3.5): a must_cover whose single-query
    #    fused top score sits below the threshold is one the corpus does not
    #    actually support.
    unmet = _detect_unmet_must_cover(
        must_cover, cover_embeddings, chunk_bm25, chunk_embeddings, cfg
    )

    return SectionResult(sections=sections, unmet_must_cover=unmet)


def _detect_unmet_must_cover(
    must_cover: list[str],
    cover_embeddings: np.ndarray,
    chunk_bm25: BM25Index,
    chunk_embeddings: np.ndarray,
    cfg: VerificationConfig,
) -> list[UnmetMustCover]:
    """Flag must_cover entries whose best single-query RRF score is too low.

    Re-fusing per-item lets the threshold be applied to a *single* must_cover's
    top evidence — a community-level fused score would average those signals
    away and miss gaps that only show up when the entry is isolated.
    """
    unmet: list[UnmetMustCover] = []
    for index, text in enumerate(must_cover):
        embedding = cover_embeddings[index : index + 1]
        fused = fused_chunk_scores_for_queries(
            [text],
            embedding,
            chunk_bm25,
            chunk_embeddings,
            cfg,
            out_size=1,
        )
        top_score = float(fused[0][1]) if fused else 0.0
        if top_score < cfg.unmet_must_cover_threshold:
            unmet.append(UnmetMustCover(index=index, text=text, top_rrf=top_score))
    return unmet


__all__ = ["run_section_pipeline"]
