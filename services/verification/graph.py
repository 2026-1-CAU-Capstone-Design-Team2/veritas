"""Shared graph utilities: similarity-graph construction + community detection.

All three tasks group items into communities — Task 1 and Task 2 group queries
by cosine similarity, Task 3 groups Key Points by an RRF-fused dual-channel
similarity. The graph *construction* differs per task, but the *community
detection* step (Louvain) is the same algorithm everywhere (VERIFY_DESIGN.md
§4.2), so it lives here instead of being copied into each pipeline.
"""

from __future__ import annotations

import networkx as nx
import numpy as np


def build_cosine_graph(embeddings: np.ndarray, edge_threshold: float) -> nx.Graph:
    """Undirected graph: one node per row, an edge wherever cosine sim >= threshold.

    ``embeddings`` must be L2-normalized so the dot product is cosine
    similarity. Every row becomes a node even when isolated, so the later
    community partition covers every item.
    """
    n = len(embeddings)
    graph = nx.Graph()
    graph.add_nodes_from(range(n))
    if n < 2:
        return graph

    matrix = np.asarray(embeddings, dtype=np.float32)
    sim = matrix @ matrix.T
    rows, cols = np.triu_indices(n, k=1)
    for i, j in zip(rows.tolist(), cols.tolist()):
        weight = float(sim[i, j])
        if weight >= edge_threshold:
            graph.add_edge(i, j, weight=weight)
    return graph


def detect_communities(graph: nx.Graph, resolution: float, seed: int = 0) -> list[list[int]]:
    """Partition ``graph`` into Louvain communities — each a sorted node-id list.

    Deterministic for a fixed ``seed``. Isolated nodes come back as singleton
    communities, so the result always partitions every node. Communities are
    returned largest-first (then by smallest member) so callers can assign
    stable ids.
    """
    if graph.number_of_nodes() == 0:
        return []
    communities = nx.community.louvain_communities(
        graph, weight="weight", resolution=resolution, seed=seed
    )
    ordered = [sorted(int(node) for node in community) for community in communities]
    ordered.sort(key=lambda members: (-len(members), members[0] if members else -1))
    return ordered


__all__ = ["build_cosine_graph", "detect_communities"]
