"""Reciprocal Rank Fusion — one function, one job (VERIFY_DESIGN.md §2.4).

RRF merges several ranked lists (e.g. a BM25 ranking and a dense ranking) into
a single fused ranking without needing the underlying scores to be
commensurable: an item's fused score is the sum of ``1 / (k + rank)`` over the
lists it appears in.
"""

from __future__ import annotations


def reciprocal_rank_fusion(
    rankings: list[list[int]],
    k: int = 60,
    out_size: int | None = None,
) -> list[tuple[int, float]]:
    """Fuse ``rankings`` (each a list of item ids, best first) into one ranking.

    Returns ``(item_id, fused_score)`` pairs sorted by score descending. Ties
    break on ``item_id`` so the output is deterministic. ``out_size`` truncates
    the result; ``None`` returns everything.
    """
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, item_id in enumerate(ranking):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank)

    fused = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return fused[:out_size] if out_size else fused


__all__ = ["reciprocal_rank_fusion"]
