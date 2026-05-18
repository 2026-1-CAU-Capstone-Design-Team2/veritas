"""Shared c-TF-IDF auto-labelling (VERIFY_DESIGN.md §3.3.3).

Each group — a report section's queries+chunks (Task 1), or a concept cluster's
Key Points (Task 3) — is treated as one pseudo-document. TF-IDF across those
pseudo-documents surfaces the terms that distinguish each group from the
others. Label terms therefore come purely from the corpus vocabulary; there is
no external label dictionary (§1.9).
"""

from __future__ import annotations

import logging
from typing import Callable

from sklearn.feature_extraction.text import TfidfVectorizer

from .models import VerificationConfig

logger = logging.getLogger(__name__)

Tokenizer = Callable[[str], list[str]]


def label_groups(
    group_texts: dict[int, str],
    tokenizer: Tokenizer,
    cfg: VerificationConfig,
) -> dict[int, list[str]]:
    """Map each group id to its top distinguishing terms.

    ``group_texts`` maps group id -> that group's joined text. Returns up to
    ``cfg.label_top_n`` terms per group, ranked by c-TF-IDF weight. A group
    whose text yields no usable vocabulary gets an empty list; if *no* group
    does, every group gets an empty list (rather than raising).
    """
    labels: dict[int, list[str]] = {gid: [] for gid in group_texts}
    usable = [gid for gid, text in group_texts.items() if text and text.strip()]
    if not usable:
        return labels

    documents = [group_texts[gid] for gid in usable]
    vectorizer = TfidfVectorizer(
        tokenizer=tokenizer,
        token_pattern=None,  # silence sklearn's "tokenizer + token_pattern" warning
        lowercase=False,     # HybridTokenizer already lower-cases
        ngram_range=(cfg.label_ngram_min, cfg.label_ngram_max),
        max_features=cfg.label_max_features,
    )
    try:
        matrix = vectorizer.fit_transform(documents).toarray()
    except ValueError as exc:
        # Raised as "empty vocabulary" when every group tokenizes to nothing.
        logger.warning("verification: c-TF-IDF labelling found no vocabulary: %s", exc)
        return labels

    terms = vectorizer.get_feature_names_out()
    for row, gid in enumerate(usable):
        weights = matrix[row]
        ranked = weights.argsort()[::-1][: cfg.label_top_n]
        labels[gid] = [terms[t] for t in ranked if weights[t] > 0.0]
    return labels


__all__ = ["label_groups"]
