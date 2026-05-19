"""Dense (embedding) retrieval channel (VERIFY_DESIGN.md §2.3).

Thin wrapper over ``llm.LLMClient`` — it only ever calls ``embed`` /
``embed_batch`` (never a text-generation endpoint, §1.2). Chunk vectors are not
re-embedded here: they already exist in ``runs/<ws>/chromadb/`` and are read out
by ``artifact_loader``. This class embeds the *queries* and *Key Points* the
tasks construct, and scores embeddings against each other.

All vectors are L2-normalized, so cosine similarity is a plain dot product —
which is what the task pipelines assume (e.g. ``chunk_emb @ q_emb``).
"""

from __future__ import annotations

from typing import Sequence

import numpy as np


def l2_normalize(matrix: np.ndarray) -> np.ndarray:
    """L2-normalize a ``(d,)`` vector or the rows of an ``(N, d)`` matrix.

    Zero vectors are left as zero (rather than producing NaNs).
    """
    arr = np.asarray(matrix, dtype=np.float32)
    if arr.ndim == 1:
        norm = float(np.linalg.norm(arr))
        return arr / norm if norm > 0.0 else arr
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return arr / norms


class DenseIndex:
    """Embeds query/Key Point text and scores embeddings — no ChromaDB state."""

    def __init__(self, llm) -> None:
        self._llm = llm

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """Embed many texts -> ``(N, d)`` L2-normalized float32 matrix."""
        items = list(texts)
        if not items:
            return np.zeros((0, 0), dtype=np.float32)
        vectors = self._llm.embed_batch(items)
        return l2_normalize(np.asarray(vectors, dtype=np.float32))

    def embed_one(self, text: str) -> np.ndarray:
        """Embed a single text -> ``(d,)`` L2-normalized float32 vector."""
        vector = np.asarray(self._llm.embed(text), dtype=np.float32)
        return l2_normalize(vector)

    @staticmethod
    def score_against(query_emb: np.ndarray, target_matrix: np.ndarray) -> np.ndarray:
        """Cosine similarity of ``query_emb`` ``(d,)`` vs each row of ``target_matrix`` ``(N, d)``.

        Both sides are assumed L2-normalized, so this is just a matrix-vector dot product.
        """
        target = np.asarray(target_matrix, dtype=np.float32)
        if target.size == 0:
            return np.zeros(0, dtype=np.float32)
        return target @ np.asarray(query_emb, dtype=np.float32)


__all__ = ["DenseIndex", "l2_normalize"]
