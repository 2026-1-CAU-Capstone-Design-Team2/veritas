"""Dual-channel concept graph over Key Points (VERIFY_DESIGN.md §5.3.2).

Builds an undirected graph where an edge's weight is the RRF fusion of two
similarity channels — dense (embedding cosine) and sparse (BM25). Fusing
*rankings* rather than raw scores sidesteps the incommensurable score scales.
Two Key Points end up linked only when both channels agree they are close,
which is what makes a resulting community a genuine "concept" rather than a
lexical or merely topical coincidence.
"""

from __future__ import annotations

import networkx as nx
import numpy as np

from ..indexing.bm25_index import BM25Index
from ..models import KeyPointRecord, VerificationConfig


def build_concept_graph(
    kp_embeddings: np.ndarray,
    kp_bm25: BM25Index,
    key_points: list[KeyPointRecord],
    cfg: VerificationConfig,
) -> nx.Graph:
    """Return the RRF-fused Key Point similarity graph (nodes are KP positions).

    An edge is kept when its symmetric fused weight clears
    ``cfg.concept_edge_threshold_rrf``. Every KP is added as a node first, so
    isolated KPs survive into the community partition as singletons.
    """
    n = len(key_points)
    graph = nx.Graph()
    graph.add_nodes_from(range(n))
    if n < 2:
        return graph

    # Dense channel: cosine similarity (embeddings are L2-normalized).
    embeddings = np.asarray(kp_embeddings, dtype=np.float32)
    sim_dense = embeddings @ embeddings.T

    # Sparse channel: BM25 of each KP's text against the KP corpus, symmetrized.
    sim_sparse = np.zeros((n, n), dtype=np.float64)
    for i, kp in enumerate(key_points):
        sim_sparse[i] = kp_bm25.score(kp.text)
    sim_sparse = (sim_sparse + sim_sparse.T) * 0.5

    # Per-row rankings -> Reciprocal Rank Fusion. argsort descending puts the
    # most similar KP first; its rank index feeds the 1/(k+rank) RRF weight.
    rank_dense = np.argsort(-sim_dense, axis=1)
    rank_sparse = np.argsort(-sim_sparse, axis=1)
    k = cfg.rrf_k
    fused = np.zeros((n, n), dtype=np.float64)
    for i in range(n):
        for rank, j in enumerate(rank_dense[i]):
            fused[i, j] += 1.0 / (k + rank)
        for rank, j in enumerate(rank_sparse[i]):
            fused[i, j] += 1.0 / (k + rank)
    fused = (fused + fused.T) * 0.5

    threshold = cfg.concept_edge_threshold_rrf
    rows, cols = np.triu_indices(n, k=1)
    for i, j in zip(rows.tolist(), cols.tolist()):
        weight = float(fused[i, j])
        if weight >= threshold:
            graph.add_edge(i, j, weight=weight)
    return graph


__all__ = ["build_concept_graph"]
