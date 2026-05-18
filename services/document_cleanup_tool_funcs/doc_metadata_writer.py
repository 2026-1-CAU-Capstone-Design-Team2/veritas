"""Write ``summary/doc_<id>.md`` directly from index.json + cleanup output.

Replaces the previous per-doc LLM summarize pass: the meta header
(Title / URL / Final URL / Domain / Search Query / Source type) is now built
deterministically from the workspace's ``summary/index.json`` record, and
``Keywords`` + ``Key Points`` come from the document_cleanup tool's JSON
output. No LLM call is needed for this writer — it is pure I/O + formatting.

The on-disk shape stays compatible with what the verification layer already
parses (``Summary`` / ``Key Points`` / ``Reliability Notes`` / ``Keywords``
section headings), with two changes:

* ``Summary`` becomes a short one-line caption built from the meta header
  (so callers that *only* read summary's first line — the verify flow
  planner doc_hints — still see something meaningful).
* ``Reliability Notes`` is omitted: the new cleanup step does not produce it
  (per the user-confirmed reduction in LLM output tokens).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


_SOURCE_TYPE_HINTS = {
    "github.com": "github",
    "githubusercontent.com": "github",
    "arxiv.org": "preprint",
    "openreview.net": "preprint",
    "wikipedia.org": "wiki",
    "medium.com": "blog",
    "substack.com": "blog",
    "dev.to": "blog",
    "stackoverflow.com": "qna",
    "reddit.com": "forum",
    "twitter.com": "social",
    "x.com": "social",
    "news.ycombinator.com": "forum",
}


def _guess_source_type(domain: str) -> str:
    """Best-effort source-type guess from the domain.

    Not used for any algorithm — only surfaced in ``doc_<id>.md`` so the
    verification UI's doc detail panel and the flow planner's doc_hints can
    show *what kind of source* the writer is looking at (blog vs wiki vs
    arXiv preprint vs forum post). Domain-suffix table is small enough to
    keep here; anything unknown falls through to ``web``.
    """
    domain = (domain or "").strip().lower()
    if not domain:
        return "web"
    for suffix, label in _SOURCE_TYPE_HINTS.items():
        if domain == suffix or domain.endswith("." + suffix):
            return label
    return "web"


def _format_meta_block(record: dict[str, Any]) -> list[str]:
    """The bullet-list header at the top of doc_<id>.md."""
    title = str(record.get("title") or "").strip() or "(제목 없음)"
    url = str(record.get("url") or "").strip()
    final_url = str(record.get("final_url") or "").strip()
    domain = str(record.get("domain") or "").strip()
    search_query = str(record.get("search_query") or "").strip()
    source_type = _guess_source_type(domain)

    rows = [f"- Title: {title}"]
    if url:
        rows.append(f"- URL: {url}")
    if final_url and final_url != url:
        rows.append(f"- Final URL: {final_url}")
    if domain:
        rows.append(f"- Domain: {domain}")
    if search_query:
        rows.append(f"- Search Query: {search_query}")
    rows.append(f"- Source type: {source_type}")
    return rows


def _format_bullet_list(items: list[str]) -> str:
    cleaned = [str(item).strip() for item in (items or []) if str(item).strip()]
    if not cleaned:
        return "(없음)"
    return "\n".join(f"- {item}" for item in cleaned)


def render_doc_md(
    record: dict[str, Any],
    *,
    keywords: list[str],
    key_points: list[str],
) -> str:
    """Render the full ``doc_<id>.md`` body for the given workspace record."""
    title = str(record.get("title") or "").strip() or f"Document {record.get('doc_id')}"
    domain = str(record.get("domain") or "").strip()

    meta_block = _format_meta_block(record)

    # The verify flow planner only reads ``Summary``'s first line, so we give
    # it a one-sentence descriptor synthesised from meta — no LLM needed.
    summary_line = (
        f"{title} — {domain}" if domain else title
    )

    sections = [
        f"# Document {record.get('doc_id')}",
        "",
        *meta_block,
        "",
        "## Summary",
        summary_line,
        "",
        "## Key Points",
        _format_bullet_list(key_points),
        "",
        "## Keywords",
        _format_bullet_list(keywords),
        "",
    ]
    return "\n".join(sections)


def write_doc_metadata(
    summary_path: Path,
    record: dict[str, Any],
    *,
    keywords: list[str],
    key_points: list[str],
) -> Path:
    """Persist a freshly built ``doc_<id>.md`` to disk and return its path."""
    body = render_doc_md(record, keywords=keywords, key_points=key_points)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(body, encoding="utf-8")
    return summary_path


__all__ = ["render_doc_md", "write_doc_metadata"]
