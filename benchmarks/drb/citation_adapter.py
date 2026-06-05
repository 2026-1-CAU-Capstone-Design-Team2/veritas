"""Export a Veritas workspace's ``final.md`` to a DRB article string.

DRB (FACT) expects an article that grounds claims with **numeric** citations
``[n]`` backed by a ``## References`` list of URLs. Veritas reports instead carry
``[doc_NNN]`` markers and a ``## Source Notes`` table. This adapter converts a
report into the DRB shape **without touching ``final.md``** — it is read-only;
all renumbering happens on an in-memory copy:

* Every ``[doc_NNN]`` (and bare ``doc_NNN`` / ``doc-NNN`` / ``doc000``) marker is
  renumbered to ``[n]`` in deterministic first-appearance order.
* A ``## References`` section is built from ``summary/index.json`` metadata,
  preferring ``final_url`` then ``url``.
* Markers inside fenced code, inline code, existing Markdown links, and bare
  URLs are left untouched so code samples and hrefs keep their literal text.
* Doc ids cited in the report but absent from ``index.json`` are still numbered
  (so the prose stays coherent) and recorded as warnings.

Pure string + file-read logic — no network, no LLM.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


_BRACKETED = r"\[doc[_-]?\d+\]"
_BARE = r"(?<![\w./:-])doc[_-]?\d+(?![\w-])"
_MARKER_RE = re.compile(rf"{_BRACKETED}|{_BARE}", re.IGNORECASE)
_HAS_DOC_RE = re.compile(r"doc[_-]?\d", re.IGNORECASE)
_DIGITS_RE = re.compile(r"\d+")

# Spans that must never be rewritten: inline code, existing Markdown
# links/images, autolinks/HTML tags, and bare URLs.
_PROTECTED_RE = re.compile(
    r"`[^`\n]*`"
    r"|!?\[[^\]]*\]\([^)]*\)"
    r"|<[^>\s]+>"
    r"|https?://\S+"
    r"|www\.\S+",
    re.IGNORECASE,
)
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_REFERENCES_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+References\b", re.IGNORECASE)
_STEM_RE = re.compile(r"^(\d+)$")


def _stem_from_marker(marker: str) -> str:
    digits = _DIGITS_RE.search(marker).group(0)  # type: ignore[union-attr]
    return f"{int(digits):03d}"


def _normalize_stem(doc_id: str) -> str | None:
    """``"000"`` / ``"7"`` → 3-digit stem; ``dup_*`` / ``fetch_error_*`` → None."""
    match = _STEM_RE.match(str(doc_id or "").strip())
    return f"{int(match.group(1)):03d}" if match else None


def load_doc_meta(index_path: str | Path) -> dict[str, dict[str, str]]:
    """Map each kept document's stem (``"000"``) to its url/title metadata.

    Duplicate and non-document rows (``dup_*`` / ``fetch_error_*``) are skipped.
    Missing/corrupt index resolves to an empty map (citations then warn).
    """
    try:
        payload = json.loads(Path(index_path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing/corrupt index is non-fatal
        return {}
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return {}
    meta: dict[str, dict[str, str]] = {}
    for record in records:
        if not isinstance(record, dict) or record.get("duplicate_of"):
            continue
        stem = _normalize_stem(record.get("doc_id"))
        if stem is None:
            continue
        meta[stem] = {
            "title": str(record.get("title") or ""),
            "url": str(record.get("url") or ""),
            "final_url": str(record.get("final_url") or ""),
            "domain": str(record.get("domain") or ""),
        }
    return meta


def _strip_existing_references(markdown: str) -> str:
    """Drop a trailing ``## References`` section (outside fences), if present."""
    lines = markdown.split("\n")
    in_fence = False
    fence_char = ""
    for i, line in enumerate(lines):
        fence = _FENCE_RE.match(line)
        if fence:
            ch = fence.group(1)[0]
            if not in_fence:
                in_fence, fence_char = True, ch
            elif ch == fence_char:
                in_fence, fence_char = False, ""
            continue
        if not in_fence and _REFERENCES_HEADING_RE.match(line):
            return "\n".join(lines[:i]).rstrip() + "\n"
    return markdown


def renumber_citations(
    markdown: str, doc_meta: dict[str, dict[str, str]]
) -> tuple[str, list[str], dict[str, int], list[str]]:
    """Renumber doc markers to ``[n]`` in first-appearance order.

    Returns ``(rewritten_markdown, ordered_stems, number_of_stem, warnings)``.
    """
    order: list[str] = []
    number_of: dict[str, int] = {}
    warnings: list[str] = []

    def assign(stem: str) -> int:
        if stem not in number_of:
            number_of[stem] = len(order) + 1
            order.append(stem)
            if stem not in doc_meta:
                warnings.append(f"unmapped doc id doc_{stem} (no index.json metadata)")
        return number_of[stem]

    out_lines: list[str] = []
    in_fence = False
    fence_char = ""
    for line in markdown.split("\n"):
        fence = _FENCE_RE.match(line)
        if fence:
            ch = fence.group(1)[0]
            if not in_fence:
                in_fence, fence_char = True, ch
            elif ch == fence_char:
                in_fence, fence_char = False, ""
            out_lines.append(line)
            continue
        out_lines.append(line if in_fence else _renumber_line(line, assign))
    return "\n".join(out_lines), order, number_of, warnings


def _renumber_line(line: str, assign) -> str:
    if not _HAS_DOC_RE.search(line):
        return line
    out: list[str] = []
    pos = 0
    for protected in _PROTECTED_RE.finditer(line):
        out.append(_renumber_chunk(line[pos : protected.start()], assign))
        out.append(protected.group(0))
        pos = protected.end()
    out.append(_renumber_chunk(line[pos:], assign))
    return "".join(out)


def _renumber_chunk(chunk: str, assign) -> str:
    if not chunk or not _HAS_DOC_RE.search(chunk):
        return chunk
    return _MARKER_RE.sub(lambda m: f"[{assign(_stem_from_marker(m.group(0)))}]", chunk)


def build_references(
    order: list[str], number_of: dict[str, int], doc_meta: dict[str, dict[str, str]]
) -> str:
    """Render a ``## References`` block, one ``[n] Title — url`` line per cite."""
    lines = ["## References", ""]
    for stem in order:
        meta = doc_meta.get(stem) or {}
        url = (meta.get("final_url") or meta.get("url") or "").strip()
        title = (meta.get("title") or "").strip() or (meta.get("domain") or "").strip()
        title = title or f"doc_{stem}"
        if url:
            lines.append(f"[{number_of[stem]}] {title} — {url}")
        else:
            lines.append(f"[{number_of[stem]}] {title} — (source URL unavailable)")
    return "\n".join(lines).rstrip() + "\n"


def export_markdown_to_article(
    final_md: str, doc_meta: dict[str, dict[str, str]]
) -> tuple[str, list[str]]:
    """Convert report Markdown to a DRB article string + warnings (pure)."""
    body_md = _strip_existing_references(final_md)
    body, order, number_of, warnings = renumber_citations(body_md, doc_meta)
    if order:
        references = build_references(order, number_of, doc_meta)
        article = body.rstrip() + "\n\n" + references
    else:
        article = body.rstrip() + "\n"
        warnings.append("no citations found in final.md")
    return article, warnings


def export_workspace_to_article(workspace_dir: str | Path) -> tuple[str, list[str]]:
    """Read a workspace's ``final.md`` + ``summary/index.json`` → DRB article.

    ``final.md`` is read only; it is never written back. Returns
    ``(article, warnings)``. Raises ``FileNotFoundError`` if ``final.md`` is
    missing (the runner records that as a task failure).
    """
    workspace = Path(workspace_dir)
    final_md = (workspace / "final.md").read_text(encoding="utf-8")
    doc_meta = load_doc_meta(workspace / "summary" / "index.json")
    return export_markdown_to_article(final_md, doc_meta)


__all__ = [
    "load_doc_meta",
    "renumber_citations",
    "build_references",
    "export_markdown_to_article",
    "export_workspace_to_article",
]
