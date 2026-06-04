from __future__ import annotations

import re


def normalize_text(text: str) -> str:
    text = str(text or "").replace("\r\n", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_markdown(
    text: str,
    *,
    max_chars: int = 1200,
    overlap_chars: int = 150,
) -> list[str]:
    text = normalize_text(text)
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    text_len = len(text)
    while start < text_len:
        end = min(start + max_chars, text_len)
        if end < text_len:
            split_at = max(
                text.rfind("\n## ", start, end),
                text.rfind("\n\n", start, end),
                text.rfind("\n", start, end),
                text.rfind(". ", start, end),
                text.rfind(" ", start, end),
            )
            if split_at > start + max_chars // 3:
                end = split_at + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= text_len:
            break
        start = max(end - overlap_chars, start + 1)
    return chunks


__all__ = ["chunk_markdown", "normalize_text"]
