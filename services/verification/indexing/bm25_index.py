"""BM25 sparse-retrieval index (VERIFY_DESIGN.md §2.2).

A thin wrapper over ``rank_bm25.BM25Okapi`` that owns tokenization through the
shared :class:`~services.verification.tokenization.HybridTokenizer`. Built in
two places — once over the chunk corpus, once over the Key Point corpus.
"""

from __future__ import annotations

from typing import Callable, Sequence

import numpy as np
from rank_bm25 import BM25Okapi

Tokenizer = Callable[[str], list[str]]


class BM25Index:
    """BM25 over a fixed corpus. Indices in scores/rankings are corpus positions.

    ``doc_ids`` (optional) lets callers map a corpus position back to a stable
    id; retrieval itself works purely on positions so it stays aligned with the
    caller's metadata list.
    """

    def __init__(self, tokenizer: Tokenizer, *, k1: float = 1.5, b: float = 0.75) -> None:
        self._tokenizer = tokenizer
        self._k1 = k1
        self._b = b
        self._bm25: BM25Okapi | None = None
        self._doc_ids: list = []
        self._size = 0

    def build(self, corpus_texts: Sequence[str], doc_ids: Sequence | None = None) -> "BM25Index":
        """Tokenize and index ``corpus_texts``. Returns ``self`` for chaining."""
        texts = list(corpus_texts)
        self._size = len(texts)
        self._doc_ids = list(doc_ids) if doc_ids is not None else list(range(self._size))
        if not texts:
            self._bm25 = None
            return self
        tokenized = [self._tokenizer(text) for text in texts]
        self._bm25 = BM25Okapi(tokenized, k1=self._k1, b=self._b)
        return self

    def score(self, query_text: str) -> np.ndarray:
        """BM25 score of ``query_text`` against every corpus item (corpus order)."""
        if self._bm25 is None:
            return np.zeros(self._size, dtype=np.float64)
        tokens = self._tokenizer(query_text)
        if not tokens:
            return np.zeros(self._size, dtype=np.float64)
        return np.asarray(self._bm25.get_scores(tokens), dtype=np.float64)

    def top_k(self, query_text: str, k: int) -> list[int]:
        """Corpus positions of the ``k`` highest-scoring items, best first."""
        scores = self.score(query_text)
        if scores.size == 0 or k <= 0:
            return []
        # Stable sort so equal scores keep corpus order — deterministic output.
        order = np.argsort(-scores, kind="stable")
        return order[:k].tolist()

    @property
    def doc_ids(self) -> list:
        return self._doc_ids

    def __len__(self) -> int:
        return self._size


__all__ = ["BM25Index"]
