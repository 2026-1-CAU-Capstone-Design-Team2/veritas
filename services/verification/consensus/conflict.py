"""Conflict-candidate detection inside a concept cluster (VERIFY_DESIGN.md §5.3.4).

Two purely geometric/statistical signals — no sentiment lexicon, no NLI model
(§10: final adjudication is left to a human or LLM, this layer only surfaces
*candidates*):

* **semantic split** — the cluster's embeddings cleanly bisect (a high
  silhouette for k=2), i.e. it actually holds two distinct sub-concepts.
* **cross-domain disagreement** — Key Points from the same domain sit closer
  together than Key Points from different domains, i.e. sources diverge.
"""

from __future__ import annotations

from itertools import combinations

import numpy as np
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score

from ..models import ConflictFlag, KeyPointRecord, VerificationConfig


def detect_conflicts(
    cluster_id: int,
    cluster: list[int],
    key_points: list[KeyPointRecord],
    kp_embeddings: np.ndarray,
    cfg: VerificationConfig,
) -> list[ConflictFlag]:
    """Return zero or more conflict flags for one concept cluster.

    ``cluster`` holds Key Point *positions* (indices into ``key_points`` /
    ``kp_embeddings``). Clusters smaller than ``cfg.conflict_min_cluster_size``
    are skipped — neither signal is meaningful on a handful of points.
    """
    flags: list[ConflictFlag] = []
    positions = sorted(cluster)
    if len(positions) < cfg.conflict_min_cluster_size:
        return flags

    sub_emb = np.asarray(kp_embeddings, dtype=np.float32)[positions]
    kp_ids = [key_points[p].kp_id for p in positions]

    # (a) Semantic split — does the cluster cleanly bisect into two sub-concepts?
    kmeans = KMeans(n_clusters=2, n_init=5, random_state=cfg.random_seed).fit(sub_emb)
    if len(set(kmeans.labels_)) == 2:
        silhouette = float(silhouette_score(sub_emb, kmeans.labels_))
        if silhouette > cfg.silhouette_split_threshold:
            flags.append(
                ConflictFlag(
                    cluster_id=cluster_id,
                    type="semantic_split",
                    score=silhouette,
                    evidence_kp_ids=list(kp_ids),
                    partition={
                        kp_id: int(label) for kp_id, label in zip(kp_ids, kmeans.labels_)
                    },
                )
            )

    # (b) Cross-domain disagreement — same-source KPs closer than cross-source KPs.
    by_domain: dict[str, list[int]] = {}
    for local_index, position in enumerate(positions):
        by_domain.setdefault(key_points[position].domain, []).append(local_index)
    if len(by_domain) >= 2:
        sim = sub_emb @ sub_emb.T
        within: list[float] = []
        between: list[float] = []
        for members in by_domain.values():
            for a, b in combinations(members, 2):
                within.append(float(sim[a, b]))
        for members_a, members_b in combinations(by_domain.values(), 2):
            for a in members_a:
                for b in members_b:
                    between.append(float(sim[a, b]))
        if within and between:
            diff = float(np.mean(within) - np.mean(between))
            if diff > cfg.cross_domain_disagreement_threshold:
                flags.append(
                    ConflictFlag(
                        cluster_id=cluster_id,
                        type="cross_domain",
                        score=diff,
                        evidence_kp_ids=list(kp_ids),
                    )
                )
    return flags


__all__ = ["detect_conflicts"]
