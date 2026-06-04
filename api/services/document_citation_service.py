"""Citation source-preview lookup for the document summary UI.

When the user clicks a ``[doc_NNN]`` citation marker rendered in the
``final.md`` summary preview, the frontend sends the *claim text* around the
marker plus the ``docId`` here. This service finds the closest
sentence/paragraph in that document's ``clean_md/<id>.md`` source using
**deterministic lexical matching only** — no LLM call, no new persisted
artifact. It exists purely as a read-only source-preview capability for the
document summary page and is intentionally kept out of the AutoSurvey,
verification, and proactive pipelines.

The public entry point is :func:`get_citation`. The scoring helpers below it
(``normalize_text`` / ``tokenize`` / ``split_paragraphs`` / ``split_sentences``
/ ``score_sentence`` / ``match_claim_in_source``) are pure functions so they
can be unit-tested without any filesystem or HTTP setup.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


# A claim is the prose around one citation marker; cap it so a runaway
# table row or merged line can't blow up the matcher or the response.
_MAX_CLAIM_CHARS = 500
# Display window for the popup — the matched sentence plus a neighbour or two.
_MAX_PARAGRAPH_CHARS = 700
_MAX_SENTENCE_CHARS = 500

# ``doc_000`` / ``000`` / ``doc-7`` → the digit run. Anything with a path
# separator, ``..`` or other noise fails this match and is rejected, which is
# the first half of the path-traversal guard (the second half re-resolves the
# final path and checks it stays under ``clean_md/``).
_DOC_ID_RE = re.compile(r"^(?:doc[_-]?)?(\d+)$", re.IGNORECASE)

_CITATION_MARKER_RE = re.compile(r"\[doc[_-]?\d+\]", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
_LATIN_RE = re.compile(r"[a-z]+")
_HANGUL_RE = re.compile(r"[가-힣]+")
# Sentence boundaries: ASCII terminators, Korean declarative/polite endings,
# and hard line breaks / list-item starts.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|(?<=다\.)\s*|(?<=요\.)\s*|\n+")


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------
def get_citation(workspace_id: str, doc_id: str, claim: str) -> dict[str, Any]:
    """Resolve a citation marker to its source snippet.

    Always returns a dict with ``docId``/``title``/``url``/``domain``/``claim``
    and a ``match`` field. ``match`` is ``None`` when the document source is
    unavailable or no claim was supplied; otherwise it is the best lexical
    candidate (even a weak one, flagged ``confidence: "low"``) so the UI can
    show a "closest source" rather than failing. Never raises for bad input —
    an invalid or path-traversal id yields ``match=None`` with an ``error`` note.
    """
    claim_text = (claim or "").strip()[:_MAX_CLAIM_CHARS]
    result: dict[str, Any] = {
        "docId": str(doc_id or ""),
        "title": "",
        "url": "",
        "domain": "",
        "claim": claim_text,
        "match": None,
    }

    stem = _normalize_doc_id(doc_id)
    if stem is None:
        result["error"] = "invalid_doc_id"
        return result

    source_path = _clean_md_path(workspace_id, stem)
    resolved_stem = stem
    if source_path is None:
        # Fall back to the zero-padded form (markers are ``doc_007`` but a bare
        # ``7`` should still resolve to ``007.md``).
        padded = f"{int(stem):03d}"
        if padded != stem:
            source_path = _clean_md_path(workspace_id, padded)
            resolved_stem = padded

    metadata = _lookup_metadata(workspace_id, {stem, resolved_stem})
    result["docId"] = f"doc_{resolved_stem}"
    if metadata:
        result["title"] = str(metadata.get("title") or "")
        result["url"] = str(metadata.get("final_url") or metadata.get("url") or "")
        result["domain"] = str(metadata.get("domain") or "")

    if source_path is None:
        result["error"] = "source_not_found"
        return result

    source_text = _read_text(source_path)
    if not source_text.strip() or not claim_text:
        return result

    result["match"] = match_claim_in_source(claim_text, source_text)
    return result


# --------------------------------------------------------------------------
# Filesystem access (kept thin; everything below is pure)
# --------------------------------------------------------------------------
def _workspace_root() -> Path:
    return Path(os.getenv("VERITAS_OUTPUT_DIR", "runs")).expanduser().resolve()


def _normalize_doc_id(raw: str) -> str | None:
    """``doc_000``/``000``/``doc-7`` → digit string; reject everything else.

    Returning ``None`` for any non-digit payload (slashes, ``..``, letters)
    is the primary path-traversal defence: the file name is built only from a
    validated digit run.
    """
    match = _DOC_ID_RE.match(str(raw or "").strip())
    return match.group(1) if match else None


def _clean_md_path(workspace_id: str, stem: str) -> Path | None:
    """Resolve ``runs/<ws>/clean_md/<stem>.md``, refusing to escape the dir.

    ``workspace_id`` is an attacker-influenceable path param, so beyond the
    digit-only ``stem`` we re-resolve the final path and confirm it still sits
    inside the workspace's ``clean_md`` directory before touching disk.
    """
    if not workspace_id or any(sep in workspace_id for sep in ("/", "\\", "..")):
        return None
    clean_dir = (_workspace_root() / workspace_id / "clean_md").resolve()
    candidate = (clean_dir / f"{stem}.md").resolve()
    if clean_dir != candidate.parent:
        return None
    return candidate if candidate.is_file() else None


def _lookup_metadata(workspace_id: str, stems: set[str]) -> dict[str, Any] | None:
    if not workspace_id or any(sep in workspace_id for sep in ("/", "\\", "..")):
        return None
    index_path = _workspace_root() / workspace_id / "summary" / "index.json"
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing/corrupt index is non-fatal
        return None
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return None
    wanted = {s.lstrip("0") or "0" for s in stems} | stems
    for record in records:
        if not isinstance(record, dict):
            continue
        rec_id = str(record.get("doc_id") or "").strip()
        if rec_id in stems or (rec_id.lstrip("0") or "0") in wanted:
            return record
    return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------
# Pure matching helpers (unit-tested directly)
# --------------------------------------------------------------------------
def normalize_text(text: str) -> str:
    """Lowercase and strip citation markers + Markdown noise; collapse spaces."""
    s = _CITATION_MARKER_RE.sub(" ", str(text or ""))
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
    strong citation signal) above ordinary word overlap.
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


def _confidence(score: float) -> str:
    if score >= 0.6:
        return "high"
    if score >= 0.3:
        return "medium"
    return "low"


def match_claim_in_source(claim: str, source_text: str) -> dict[str, Any] | None:
    """Find the best-matching sentence for *claim* inside *source_text*.

    Returns the match record (``text`` / ``paragraphText`` / ``paragraphIndex``
    / ``sentenceIndex`` / ``score`` / ``confidence``) for the top candidate, or
    ``None`` when the source has no usable sentence. A weak best match is still
    returned, flagged ``confidence: "low"`` — the UI presents it as the closest
    candidate rather than a confirmed source.
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
                    "confidence": _confidence(score),
                }
    return best


def _window(sentences: list[str], index: int) -> str:
    """Matched sentence plus up to one neighbour each side, capped for the popup."""
    start = max(0, index - 1)
    snippet = " ".join(sentences[start : index + 2]).strip()
    if len(snippet) <= _MAX_PARAGRAPH_CHARS:
        return snippet
    # Too long with neighbours — fall back to the matched sentence alone.
    return sentences[index].strip()[:_MAX_PARAGRAPH_CHARS]


__all__ = [
    "get_citation",
    "normalize_text",
    "tokenize",
    "split_paragraphs",
    "split_sentences",
    "score_sentence",
    "match_claim_in_source",
]
