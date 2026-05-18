"""Corpus-internal domain authority via HITS (VERIFY_DESIGN.md §5.3.3).

There is no external trust whitelist (§1.9, §11). A domain's authority is
derived purely from how its Key Points co-cluster with everyone else's: a
domain whose claims keep landing in large, cross-source concept clusters
accumulates authority; an isolated source does not. The signal is a HITS run
over a bipartite domain<->Key-Point graph whose edges only exist inside concept
clusters and are weighted by cluster size.
"""

from __future__ import annotations

import networkx as nx

from ..models import KeyPointRecord, VerificationConfig


def compute_domain_authority(
    key_points: list[KeyPointRecord],
    concept_clusters: list[list[int]],
    cfg: VerificationConfig,
) -> dict[str, float]:
    """Map each domain to a HITS authority score.

    ``concept_clusters`` holds Key Point *positions* (indices into
    ``key_points``). Domains that never co-cluster still appear in the result,
    with ~0 authority, so callers can rely on every domain being present.
    """
    domains = sorted({kp.domain for kp in key_points if kp.domain})
    if not domains:
        return {}

    bipartite = nx.Graph()
    bipartite.add_nodes_from(("domain", domain) for domain in domains)
    bipartite.add_nodes_from(("kp", position) for position in range(len(key_points)))

    for cluster in concept_clusters:
        if len(cluster) < cfg.min_cluster_size:
            continue
        # Larger agreed-upon clusters pass more authority to their domains.
        weight = 1.0 + 0.1 * len(cluster)
        for position in cluster:
            domain = key_points[position].domain
            if domain:
                bipartite.add_edge(("domain", domain), ("kp", position), weight=weight)

    if bipartite.number_of_edges() == 0:
        return {domain: 0.0 for domain in domains}

    try:
        _hubs, authority = nx.hits(bipartite, max_iter=cfg.hits_max_iter, normalized=True)
    except nx.PowerIterationFailedConvergence:
        return {domain: 0.0 for domain in domains}

    # HITS authority is non-negative by definition; clamp away the tiny
    # negative / NaN values its power iteration can leave behind near zero.
    scores: dict[str, float] = {}
    for domain in domains:
        value = float(authority.get(("domain", domain), 0.0))
        scores[domain] = value if value > 0.0 else 0.0
    return scores


__all__ = ["compute_domain_authority"]
