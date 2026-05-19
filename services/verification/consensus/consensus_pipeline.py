"""Task 3 entry point — assemble cross-source consensus (VERIFY_DESIGN.md §5).

This module only *orchestrates*: every algorithm body lives in a sibling module
(``concept_graph``, ``authority``, ``conflict``) or a shared one (``graph``,
``labeling``). §1.1.

Pipeline: embed Key Points -> RRF-fused concept graph -> Louvain communities ->
per-cluster metrics (diversity, PageRank centrality, domain authority) and
conflict candidates.
"""

from __future__ import annotations

import networkx as nx
import numpy as np

from ..graph import detect_communities
from ..indexing.bm25_index import BM25Index
from ..indexing.dense_index import DenseIndex
from ..labeling import label_groups
from ..models import ConceptCluster, ConsensusResult, KeyPointRecord, VerificationConfig
from ..tokenization import HybridTokenizer
from .authority import compute_domain_authority
from .concept_graph import build_concept_graph
from .conflict import detect_conflicts


def _shannon_diversity(domains: list[str]) -> float:
    """Shannon entropy (nats) of a cluster's domain distribution.

    0 when every Key Point is from one source; ``ln(n_domains)`` when they are
    spread evenly. This is the cross-source spread signal — a concept agreed on
    by many independent domains scores higher.
    """
    if not domains:
        return 0.0
    counts: dict[str, int] = {}
    for domain in domains:
        counts[domain] = counts.get(domain, 0) + 1
    total = len(domains)
    entropy = 0.0
    for count in counts.values():
        p = count / total
        entropy -= p * np.log(p)
    return float(entropy)


def _safe_pagerank(graph: nx.Graph) -> dict[int, float]:
    """PageRank over the concept graph; falls back to uniform on non-convergence."""
    if graph.number_of_nodes() == 0:
        return {}
    try:
        return nx.pagerank(graph, weight="weight")
    except nx.PowerIterationFailedConvergence:
        uniform = 1.0 / graph.number_of_nodes()
        return {node: uniform for node in graph.nodes()}


def run_consensus_pipeline(
    kps: list[KeyPointRecord],
    kp_bm25: BM25Index,
    dense: DenseIndex,
    cfg: VerificationConfig,
    tokenizer: HybridTokenizer | None = None,
) -> ConsensusResult:
    """Cluster Key Points into concepts, score domain authority, flag conflicts.

    ``kp_bm25`` is expected to be already built over ``[kp.text for kp in kps]``
    (the facade owns index construction, §1.4). ``tokenizer`` is reused for
    c-TF-IDF labelling; one is created if not supplied.
    """
    result = ConsensusResult()
    if not kps:
        return result

    tokenizer = tokenizer or HybridTokenizer()

    # 1. Dense channel — embed every Key Point. Cache the vector on the record.
    kp_embeddings = dense.embed([kp.text for kp in kps])
    for kp, vector in zip(kps, kp_embeddings):
        kp.embedding = vector

    # 2. RRF-fused concept graph -> Louvain communities, keeping only real clusters.
    graph = build_concept_graph(kp_embeddings, kp_bm25, kps, cfg)
    communities = detect_communities(graph, cfg.community_resolution, seed=cfg.random_seed)
    clusters = [c for c in communities if len(c) >= cfg.min_cluster_size]

    # 3. Node-level PageRank centrality over the concept graph.
    pagerank = _safe_pagerank(graph)

    # 4. Corpus-internal domain authority (HITS over the bipartite graph).
    result.domain_authority = compute_domain_authority(kps, clusters, cfg)

    # 5. Auto-label every cluster from its own Key Point vocabulary (c-TF-IDF).
    cluster_texts = {
        cid: "\n".join(kps[position].text for position in cluster)
        for cid, cluster in enumerate(clusters)
    }
    cluster_labels = label_groups(cluster_texts, tokenizer, cfg)

    # 6. Per-cluster metrics + conflict candidates.
    for cid, cluster in enumerate(clusters):
        domains = [kps[position].domain for position in cluster if kps[position].domain]
        diversity = _shannon_diversity(domains)
        # Mean (not sum) PageRank: a size-neutral "how central are these KPs"
        # score, so composite ranks concept *quality*, not just cluster size.
        centrality = float(np.mean([pagerank.get(p, 0.0) for p in cluster])) if cluster else 0.0
        authority_mean = (
            float(np.mean([result.domain_authority.get(d, 0.0) for d in domains]))
            if domains
            else 0.0
        )
        result.concept_clusters.append(
            ConceptCluster(
                id=cid,
                label_terms=cluster_labels.get(cid, []),
                kp_ids=[kps[position].kp_id for position in cluster],
                domains=sorted(set(domains)),
                pagerank=centrality,
                diversity=diversity,
                authority_mean=authority_mean,
                composite=centrality * diversity * authority_mean,
            )
        )
        result.conflicts.extend(detect_conflicts(cid, cluster, kps, kp_embeddings, cfg))

    return result


__all__ = ["run_consensus_pipeline"]
