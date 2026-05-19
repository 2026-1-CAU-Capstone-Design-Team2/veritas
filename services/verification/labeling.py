"""Shared c-TF-IDF auto-labelling (VERIFY_DESIGN.md §3.3.3).

Each group — a report section's queries+chunks (Task 1), or a concept cluster's
Key Points (Task 3) — is treated as one pseudo-document. TF-IDF across those
pseudo-documents surfaces the terms that distinguish each group from the
others. Label terms therefore come purely from the corpus vocabulary; there is
no external label dictionary (§1.9).

c-TF-IDF degenerates when there is only *one* pseudo-document — without a
second document there is no IDF signal, so every frequent token (including
function words and URL fragments) ends up with the same weight. The label
filter at the bottom of this module compensates: it drops generic stop-words,
URL/domain fragments, pure numerals and very short tokens *after* sklearn has
ranked them, so the returned list always reads as meaningful content terms.
"""

from __future__ import annotations

import logging
import re
from typing import Callable

from sklearn.feature_extraction.text import TfidfVectorizer

from .models import VerificationConfig

logger = logging.getLogger(__name__)

Tokenizer = Callable[[str], list[str]]


# Generic English function words and web-noise tokens. Intentionally small —
# this is *not* a domain stop-list. Anything that would appear on virtually
# every web doc regardless of topic belongs here.
_GENERIC_STOPWORDS: frozenset[str] = frozenset(
    {
        # English function words & extreme high-frequency
        "the", "and", "or", "to", "of", "in", "is", "a", "an", "for", "on",
        "with", "by", "as", "at", "be", "this", "that", "from", "it", "are",
        "was", "we", "you", "i", "they", "he", "she", "his", "her", "its",
        "our", "their", "your", "but", "not", "no", "do", "does", "did",
        "have", "has", "had", "will", "would", "can", "could", "should",
        "may", "might", "than", "then", "so", "if", "when", "while", "where",
        "what", "which", "who", "whom", "how", "why", "into", "out", "up",
        "down", "over", "under", "about", "after", "before", "between",
        "through", "more", "most", "some", "such", "any", "all", "each",
        "other", "another", "one", "two", "also", "only", "even",
        # Web noise / URL fragments
        "https", "http", "www", "com", "org", "io", "net", "info", "html",
        "github", "pdf", "blog", "docs", "doc", "page", "site", "link",
        "url", "href", "src",
        # Korean function-like fragments that occasionally survive Kiwi
        "것", "수", "등", "이", "그", "저", "때", "곳", "점", "건",
    }
)

# Tokens that look like URLs, domains, file paths or numeric IDs — never
# useful as a label even if c-TF-IDF ranks them highly.
_URL_TOKEN = re.compile(r"^[a-z0-9]+(\.[a-z0-9]+)+$")  # x.y, foo.bar.baz
_PATH_TOKEN = re.compile(r"[\\/]")
_LONG_DIGIT = re.compile(r"^\d{3,}$")


def _is_meaningful_label(term: str) -> bool:
    """Heuristic 'is this term worth showing as a label?' filter.

    Applied to every c-TF-IDF candidate. The rules are deliberately
    language-agnostic geometry / regex checks so no domain vocabulary leaks
    in (§1.9). For n-grams the filter runs against *every* sub-token —
    otherwise a bigram like ``"https modelcontextprotocol.info"`` would slip
    past simply because the whole string isn't itself a stop-word.
    """
    text = term.strip()
    if not text or len(text) < 2:
        return False
    if " " in text:
        # n-gram: every sub-token must independently be a meaningful label,
        # and at least one of them has to carry real content (otherwise pure
        # stop-word combos would still pass).
        parts = [part for part in text.split() if part]
        if not parts:
            return False
        return all(_is_meaningful_label(part) for part in parts)
    lower = text.lower()
    if lower in _GENERIC_STOPWORDS:
        return False
    if _URL_TOKEN.match(lower):
        return False
    if _PATH_TOKEN.search(lower):
        return False
    if _LONG_DIGIT.match(lower):
        return False
    # Single-character CJK tokens are usually function morphemes Kiwi let
    # through (의/와/는/…). Allow 2+ char CJK; allow 2+ ASCII.
    if all("가" <= ch <= "힯" or "一" <= ch <= "鿿" for ch in text):
        return len(text) >= 2
    if all(ch.isascii() and (ch.isalnum() or ch in "-_") for ch in text):
        return len(text) >= 2
    return True


def _filtered_terms(
    weights,
    terms: list[str],
    top_n: int,
) -> list[str]:
    """Pick the top ``top_n`` *meaningful* terms by c-TF-IDF weight."""
    out: list[str] = []
    # ``argsort`` ascending → reverse for descending. We over-fetch
    # (10× top_n, capped at vocabulary size) to make sure enough survive
    # the meaningfulness filter when noise dominates the top of the list.
    over_fetch = min(len(terms), max(top_n * 10, top_n))
    candidates = weights.argsort()[::-1][:over_fetch]
    seen: set[str] = set()
    for idx in candidates:
        if weights[idx] <= 0.0:
            break
        term = str(terms[idx]).strip()
        key = term.lower()
        if key in seen:
            continue
        if not _is_meaningful_label(term):
            continue
        seen.add(key)
        out.append(term)
        if len(out) >= top_n:
            break
    return out


def label_groups(
    group_texts: dict[int, str],
    tokenizer: Tokenizer,
    cfg: VerificationConfig,
) -> dict[int, list[str]]:
    """Map each group id to its top distinguishing terms.

    ``group_texts`` maps group id → its joined text. Returns up to
    ``cfg.label_top_n`` *meaningful* terms per group ranked by c-TF-IDF.
    Generic stop-words, URL fragments and short / numeric tokens are filtered
    out after ranking so they never show up as section labels — see
    :func:`_is_meaningful_label`.
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

    terms = list(vectorizer.get_feature_names_out())
    for row, gid in enumerate(usable):
        labels[gid] = _filtered_terms(matrix[row], terms, cfg.label_top_n)
    return labels


__all__ = ["label_groups"]
