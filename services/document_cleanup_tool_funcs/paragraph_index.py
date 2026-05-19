"""Stable Markdown paragraph splitting + indexed-prompt assembly + removal.

The LLM cleanup pass works on *paragraphs* — each paragraph gets a stable
``[P0]``, ``[P1]``, … prefix in the prompt, the LLM returns which indices
are boilerplate, and we drop those paragraphs to produce ``clean_md``.

Paragraph boundary = one or more blank lines (the standard Markdown
definition). We intentionally do NOT split on headings or list markers
themselves — keeping a heading attached to its body paragraph means the
LLM cannot accidentally keep a heading while removing its body (or vice
versa) and the resulting clean Markdown stays syntactically valid.
"""

from __future__ import annotations

import re

# Paragraph boundary — one or more blank lines (with optional whitespace).
_PARA_BOUNDARY = re.compile(r"\n\s*\n+")


def split_paragraphs(markdown: str) -> list[str]:
    """Split a Markdown document into paragraph blocks.

    Empty blocks are dropped. Each block is returned with leading / trailing
    whitespace stripped but its internal line breaks preserved so a list or
    a fenced code block survives intact.
    """
    if not markdown:
        return []
    blocks: list[str] = []
    for raw in _PARA_BOUNDARY.split(markdown):
        block = raw.strip("\n")
        if block.strip():
            blocks.append(block)
    return blocks


def annotate_paragraphs(paragraphs: list[str]) -> str:
    """Render the paragraph list as a numbered prompt body for the LLM.

    Each paragraph is prefixed by ``[P<i>]`` on its own line, followed by the
    paragraph content. Two blank lines between paragraphs preserve the
    boundary the LLM can re-read.
    """
    lines: list[str] = []
    for index, paragraph in enumerate(paragraphs):
        lines.append(f"[P{index}]")
        lines.append(paragraph)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def apply_boilerplate_removal(
    paragraphs: list[str],
    boilerplate_indices: list[int],
) -> str:
    """Return the body Markdown with the LLM-flagged paragraphs removed.

    Out-of-range indices are silently ignored (an over-zealous model emitting
    indices past the end shouldn't crash the pipeline). The result is joined
    back with one blank line between paragraphs — standard Markdown spacing.
    """
    drop: set[int] = {int(i) for i in boilerplate_indices if isinstance(i, int) and 0 <= int(i) < len(paragraphs)}
    kept = [p for index, p in enumerate(paragraphs) if index not in drop]
    if not kept:
        return ""
    return "\n\n".join(kept).rstrip() + "\n"


__all__ = [
    "split_paragraphs",
    "annotate_paragraphs",
    "apply_boilerplate_removal",
]
