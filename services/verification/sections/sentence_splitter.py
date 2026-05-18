"""Split each doc's clean_md into stable, addressable sentence units.

Used by the new sentence-flow Task 1 (VERIFY_DESIGN.md §3, redesigned): the
report-flow outline assigns *sentences* (not whole docs) to each ordered
section, so we need a deterministic decomposition of every doc into

    (doc_id, paragraph_index, sentence_index, text)

units. Paragraphs are blank-line separated blocks of the clean Markdown.
Within a paragraph, Kiwi splits Korean sentences and a regex pass splits
English sentences in mixed-language paragraphs — the goal is a sentence-ish
unit users will recognise in the source, not a perfect linguistic split.

Markdown noise (headings, code fences, list bullets, tables) is stripped at
the paragraph boundary so the output reads as body text, not as raw .md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from kiwipiepy import Kiwi

from ..models import DocRecord, SentenceUnit, VerificationConfig

# Lines we never want as sentence content. Markdown headings, horizontal
# rules, table rows, code fences, blockquote markers and pure-link lines.
_NOISE_LINE_RE = re.compile(
    r"^(?:#{1,6}\s|>+\s?|[\-=*_]{3,}\s*$|\|.*\||```|~~~|<\w+|</\w+|\[!\[)",
    re.MULTILINE,
)
# Bullet / numbered list markers at line start — keep the content, drop the marker.
_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+\.\s+)", re.MULTILINE)
# Markdown link `[text](url)` → `text`; collapses inline links so the
# sentence text doesn't carry raw URLs.
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
# Inline code, italics, bold, strikethrough markers.
_MD_FORMAT_RE = re.compile(r"[`*_~]+")
# Raw URLs that survived the link strip (bare http(s)://… runs).
_BARE_URL_RE = re.compile(r"https?://\S+")
# Paragraph boundary — one or more blank lines.
_PARA_RE = re.compile(r"\n{2,}")
# English-side sentence boundary: ., !, ? followed by whitespace + capital
# letter / digit / Korean. Deliberately permissive — we only need *plausible*
# boundaries, not perfect ones.
_EN_SENT_RE = re.compile(r"(?<=[\.!?])\s+(?=[A-Z0-9가-힣])")


@dataclass(frozen=True)
class _SplitContext:
    min_chars: int
    max_chars: int


def _strip_markdown(line: str) -> str:
    """Replace Markdown formatting noise on one line."""
    cleaned = _MD_LINK_RE.sub(r"\1", line)
    cleaned = _MD_FORMAT_RE.sub("", cleaned)
    cleaned = _BARE_URL_RE.sub("", cleaned)
    cleaned = _LIST_MARKER_RE.sub("", cleaned)
    return cleaned.strip()


def _paragraph_blocks(text: str) -> list[str]:
    """Split a doc's clean_md into paragraph text, dropping pure-noise blocks.

    Each returned block has Markdown formatting flattened and noise lines
    (headings, code fences, table rows, …) removed. Empty blocks are
    discarded so paragraph indices match what the user would read.
    """
    if not text:
        return []
    blocks: list[str] = []
    for raw_block in _PARA_RE.split(text):
        lines: list[str] = []
        for line in raw_block.splitlines():
            if _NOISE_LINE_RE.match(line.strip()):
                continue
            cleaned = _strip_markdown(line)
            if cleaned:
                lines.append(cleaned)
        if not lines:
            continue
        merged = " ".join(lines).strip()
        if merged:
            blocks.append(merged)
    return blocks


def _split_paragraph(paragraph: str, kiwi: Kiwi, ctx: _SplitContext) -> list[str]:
    """Sentence-ish units from one paragraph. Kiwi first, regex as fallback."""
    try:
        kiwi_sents = [str(getattr(s, "text", s) or "").strip() for s in kiwi.split_into_sents(paragraph)]
    except Exception:
        kiwi_sents = [paragraph.strip()]

    expanded: list[str] = []
    for sent in kiwi_sents:
        if not sent:
            continue
        # Kiwi can leave a long English run as one sentence — re-split it.
        for piece in _EN_SENT_RE.split(sent):
            piece = piece.strip()
            if not piece:
                continue
            # If it's *still* too long, chop on the last terminal punctuation
            # before max_chars; otherwise keep as-is (a paragraph-long sentence
            # is rare but legitimate in legal/academic prose).
            while len(piece) > ctx.max_chars:
                cut = max(
                    piece.rfind(". ", 0, ctx.max_chars),
                    piece.rfind("? ", 0, ctx.max_chars),
                    piece.rfind("! ", 0, ctx.max_chars),
                    piece.rfind("; ", 0, ctx.max_chars),
                    piece.rfind(", ", 0, ctx.max_chars),
                )
                if cut <= 0:
                    cut = ctx.max_chars
                expanded.append(piece[: cut + 1].strip())
                piece = piece[cut + 1 :].strip()
                if not piece:
                    break
            if piece:
                expanded.append(piece)
    return [s for s in expanded if len(s) >= ctx.min_chars]


def split_docs_to_sentences(
    docs: list[DocRecord],
    cfg: VerificationConfig,
    kiwi: Kiwi | None = None,
) -> list[SentenceUnit]:
    """Decompose every non-duplicate doc into ordered sentence units.

    ``order`` is a corpus-wide rank used as the deterministic sort position
    for retrieval rankings. Duplicate docs (``is_duplicate=True``) have empty
    clean_md and are skipped here even if they slipped through the loader.
    """
    kiwi = kiwi or Kiwi()
    ctx = _SplitContext(
        min_chars=int(cfg.section_sentence_min_chars),
        max_chars=int(cfg.section_sentence_max_chars),
    )
    out: list[SentenceUnit] = []
    order = 0
    for doc in docs:
        if doc.is_duplicate or not doc.clean_md_text:
            continue
        paragraphs = _paragraph_blocks(doc.clean_md_text)
        for para_idx, paragraph in enumerate(paragraphs):
            sentences = _split_paragraph(paragraph, kiwi, ctx)
            for sent_idx, text in enumerate(sentences):
                out.append(
                    SentenceUnit(
                        doc_id=doc.doc_id,
                        paragraph_index=para_idx,
                        sentence_index=sent_idx,
                        text=text,
                        order=order,
                    )
                )
                order += 1
    return out


__all__ = ["split_docs_to_sentences"]
