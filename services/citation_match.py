"""Deterministic lexical matching for citation source-preview.

These are the **single source of truth** for the citation matcher: pure,
side-effect-free scoring functions shared by the read-only citation lookup
(``api/services/document_citation_service.py``) and the AutoSurvey-side
citation-evidence builder (``services/citation_evidence.py``). They live here,
under ``services/``, so the pipeline can reuse them without importing ``api/``.

Everything is general signal only — exact/substring containment, content-token
overlap, and shared numeric figures. There are no keyword dictionaries, no
site- or language-specific rules, and no sentinel values that special-case a
particular input; the only constants are continuous score thresholds and length
caps. Korean and Latin text are tokenized structurally (syllable/word runs),
which lets a Korean claim score against Korean source text and a figure (a
strong citation signal) score across languages.
"""

from __future__ import annotations

import re
from typing import Any


# Display window for the popup — the matched sentence plus a neighbour or two.
_MAX_PARAGRAPH_CHARS = 700
_MAX_SENTENCE_CHARS = 500

# A bracketed ``[doc_NNN]`` marker (also reused by the batch-anchor stripper).
CITATION_MARKER_RE = re.compile(r"\[doc[_-]?\d+\]", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
_LATIN_RE = re.compile(r"[a-z]+")
_HANGUL_RE = re.compile(r"[가-힣]+")
# Sentence boundaries: ASCII terminators, Korean declarative/polite endings,
# and hard line breaks / list-item starts.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|(?<=다\.)\s*|(?<=요\.)\s*|\n+")


def normalize_text(text: str) -> str:
    """Lowercase and strip citation markers + Markdown noise; collapse spaces."""
    s = CITATION_MARKER_RE.sub(" ", str(text or ""))
    # Markdown link → keep the visible label, drop the target.
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = s.lower()
    # Heading/bullet/table/quote/emphasis/code punctuation → space.
    s = re.sub(r"[#>*`~|]+", " ", s)
    s = re.sub(r"(^|\s)[-+]\s", " ", s)
    s = s.replace("​", "").replace("﻿", "")
    return re.sub(r"\s+", " ", s).strip()


def tokenize(text: str) -> tuple[set[str], set[str]]:
    """Return ``(content_tokens, number_tokens)`` from normalized text.

    Content tokens are Latin words and Korean syllable runs of length ≥ 2;
    numbers are kept separately so the scorer can weight a shared figure (a
    strong citation signal) above ordinary word overlap. The ≥ 2 rule drops
    one-syllable Korean particles, so ``대체육은`` contributes the ``대체육``
    run and still matches a topic of ``대체육``.
    """
    norm = normalize_text(text)
    content = {t for t in _LATIN_RE.findall(norm) if len(t) >= 2}
    content |= {t for t in _HANGUL_RE.findall(norm) if len(t) >= 2}
    numbers = set(_NUMBER_RE.findall(norm))
    return content, numbers


def split_paragraphs(text: str) -> list[str]:
    """Split on blank-line boundaries; drop empties."""
    blocks = re.split(r"\n\s*\n", str(text or ""))
    return [b.strip() for b in blocks if b.strip()]


def split_sentences(paragraph: str) -> list[str]:
    """Lightweight sentence split (ASCII + Korean endings + line breaks)."""
    parts = _SENTENCE_SPLIT_RE.split(str(paragraph or ""))
    return [p.strip() for p in parts if p and p.strip()]


def confidence_for(score: float) -> str:
    if score >= 0.6:
        return "high"
    if score >= 0.3:
        return "medium"
    return "low"


def _is_boilerplate(sentence: str, content: set[str], numbers: set[str]) -> bool:
    norm = normalize_text(sentence)
    if len(norm) < 10:
        return True
    return len(content) + len(numbers) < 2


def score_sentence(
    claim_norm: str,
    claim_content: set[str],
    claim_numbers: set[str],
    sentence: str,
) -> float:
    """Score one source *sentence* against the (pre-normalized) claim → 0..1.

    Exact normalized-substring containment dominates; otherwise the score is a
    blend of content-token overlap and (when the claim carries figures) numeric
    overlap. Pure and side-effect free.
    """
    sent_norm = normalize_text(sentence)
    if not sent_norm or (not claim_content and not claim_numbers):
        return 0.0
    sent_content, sent_numbers = tokenize(sentence)

    tok_overlap = (
        len(claim_content & sent_content) / len(claim_content)
        if claim_content
        else 0.0
    )
    if claim_numbers:
        num_overlap = len(claim_numbers & sent_numbers) / len(claim_numbers)
        score = 0.55 * tok_overlap + 0.45 * num_overlap
    else:
        score = tok_overlap

    if len(claim_norm) >= 8 and claim_norm in sent_norm:
        exact = 0.85 + 0.15 * min(1.0, len(claim_norm) / 120.0)
        score = max(score, exact)
    return max(0.0, min(1.0, score))


def _window(sentences: list[str], index: int) -> str:
    """Matched sentence plus up to one neighbour each side, capped for the popup."""
    start = max(0, index - 1)
    snippet = " ".join(sentences[start : index + 2]).strip()
    if len(snippet) <= _MAX_PARAGRAPH_CHARS:
        return snippet
    # Too long with neighbours — fall back to the matched sentence alone.
    return sentences[index].strip()[:_MAX_PARAGRAPH_CHARS]


def match_claim_in_source(claim: str, source_text: str) -> dict[str, Any] | None:
    """Find the best-matching sentence for *claim* inside *source_text*.

    Returns the match record (``text`` / ``paragraphText`` / ``paragraphIndex``
    / ``sentenceIndex`` / ``score`` / ``confidence``) for the top candidate, or
    ``None`` when the source has no usable sentence. A weak best match is still
    returned, flagged ``confidence: "low"`` — the caller decides whether to use
    it or fall back.
    """
    claim_norm = normalize_text(claim)
    claim_content, claim_numbers = tokenize(claim)

    paragraphs = split_paragraphs(source_text)
    best: dict[str, Any] | None = None
    best_score = -1.0
    for p_index, paragraph in enumerate(paragraphs):
        sentences = split_sentences(paragraph)
        for s_index, sentence in enumerate(sentences):
            if _is_boilerplate(sentence, *tokenize(sentence)):
                continue
            score = score_sentence(claim_norm, claim_content, claim_numbers, sentence)
            if score > best_score:
                best_score = score
                best = {
                    "text": sentence[:_MAX_SENTENCE_CHARS],
                    "paragraphText": _window(sentences, s_index),
                    "paragraphIndex": p_index,
                    "sentenceIndex": s_index,
                    "score": round(score, 4),
                    "confidence": confidence_for(score),
                }
    return best


def claim_overlap(content: set[str], numbers: set[str], other: str) -> float:
    """Fraction of the claim's content+number tokens that appear in *other*.

    Used to test how related two *claims* are (e.g. a clicked final claim vs a
    batch finding or an evidence atom's localized claim), independent of any
    source text. Pure; symmetric only when the token sets are the same size.
    """
    denom = len(content) + len(numbers)
    if denom == 0:
        return 0.0
    other_content, other_numbers = tokenize(other)
    shared = len(content & other_content) + len(numbers & other_numbers)
    return shared / denom


__all__ = [
    "CITATION_MARKER_RE",
    "normalize_text",
    "tokenize",
    "split_paragraphs",
    "split_sentences",
    "score_sentence",
    "match_claim_in_source",
    "claim_overlap",
    "confidence_for",
]
