"""Flat (one-shot) research baseline for the DRB comparison.

This is the control arm: the *same* generator model and the *same* web-search /
fetch primitives as Veritas AutoSurvey, but **without** the iterative design —
no source-quality gate, no cleanup, no batch summaries, no gap analysis, no
replan loop, no RAG indexing, no final-report normalizer. The pipeline is:

    plan queries → search → dedupe URLs → fetch ≤ max_docs → one report call

It is written against **injected callables** (``query_fn`` / ``search_fn`` /
``fetch_fn`` / ``report_fn``) so it unit-tests with fakes and never imports any
AutoSurvey orchestration or tool. The real wiring (LLM client + DuckDuckGo +
Crawl4AI) is assembled in ``flat_runner.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable


# A flat article that already starts a "## References" block from the model is
# replaced with a deterministic one built from the actually-fetched sources, so
# URLs are never invented and both arms get harness-built references.
_REF_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+References\b", re.IGNORECASE)


@dataclass
class FlatSource:
    number: int
    url: str
    title: str
    text: str


@dataclass
class FlatResult:
    article: str
    sources: list[FlatSource]
    queries: list[str]
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)


def build_sources_block(sources: list[FlatSource], *, per_source_chars: int) -> str:
    """Render the numbered source packet handed to the report model."""
    blocks: list[str] = []
    for source in sources:
        head = f"[{source.number}] {source.title or source.url}\nURL: {source.url}"
        body = (source.text or "")[:per_source_chars].strip()
        blocks.append(f"{head}\n{body}".rstrip())
    return "\n\n---\n\n".join(blocks)


def _build_references(sources: list[FlatSource]) -> str:
    lines = ["## References", ""]
    for source in sources:
        title = (source.title or "").strip() or source.url
        lines.append(f"[{source.number}] {title} — {source.url}")
    return "\n".join(lines).rstrip() + "\n"


def _strip_model_references(markdown: str) -> str:
    """Cut a trailing ``## References`` the model may have written itself."""
    out: list[str] = []
    for line in str(markdown or "").split("\n"):
        if _REF_HEADING_RE.match(line):
            break
        out.append(line)
    return "\n".join(out).rstrip()


def finalize_article(report_body: str, sources: list[FlatSource]) -> str:
    """Attach a deterministic, URL-bearing reference list to the report body."""
    body = _strip_model_references(report_body)
    if not sources:
        return body + "\n"
    return body + "\n\n" + _build_references(sources)


def _dedupe_search_urls(
    queries: list[str],
    search_fn: Callable[[str, int], list[dict[str, Any]]],
    *,
    results_per_query: int,
) -> list[str]:
    """Run each query and collect unique result URLs in first-seen order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for query in queries:
        for result in search_fn(query, results_per_query) or []:
            url = str((result or {}).get("link") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            ordered.append(url)
    return ordered


def run_flat_research(
    task_prompt: str,
    *,
    language: str = "",
    query_fn: Callable[[str, str, int], list[str]],
    search_fn: Callable[[str, int], list[dict[str, Any]]],
    fetch_fn: Callable[[str, int], dict[str, Any]],
    report_fn: Callable[[str, str, str], str],
    max_docs: int = 15,
    search_query_count: int = 5,
    results_per_query: int = 10,
    fetch_max_chars: int = 100_000,
    per_source_chars: int | None = None,
) -> FlatResult:
    """Run the flat baseline for one task and return the article + stats.

    ``query_fn(task_prompt, language, max_queries) -> list[str]``
    ``search_fn(query, num_results) -> [{"title","link","snippet"}, ...]``
    ``fetch_fn(url, max_chars) -> {"success","text","final_url","title","domain"}``
    ``report_fn(task_prompt, language, sources_block) -> str``
    """
    warnings: list[str] = []

    queries = [q for q in (query_fn(task_prompt, language, search_query_count) or []) if str(q).strip()]
    queries = queries[:search_query_count]
    if not queries:
        queries = [task_prompt]
        warnings.append("query planning returned nothing; fell back to the task prompt")

    candidate_urls = _dedupe_search_urls(
        queries, search_fn, results_per_query=results_per_query
    )

    sources: list[FlatSource] = []
    fetch_errors = 0
    for url in candidate_urls:
        if len(sources) >= max_docs:
            break
        doc = fetch_fn(url, fetch_max_chars) or {}
        if not doc.get("success") or not str(doc.get("text") or "").strip():
            fetch_errors += 1
            continue
        sources.append(
            FlatSource(
                number=len(sources) + 1,
                url=str(doc.get("final_url") or doc.get("url") or url),
                title=str(doc.get("title") or ""),
                text=str(doc.get("text") or ""),
            )
        )
    if not sources:
        warnings.append("no documents were fetched")

    # Share the fetch budget across the packet so the report prompt stays within
    # context: each source gets an even slice of the per-document cap.
    per_source = per_source_chars or max(1000, fetch_max_chars // max(1, max_docs))
    sources_block = build_sources_block(sources, per_source_chars=per_source)

    report_body = report_fn(task_prompt, language, sources_block) or ""
    article = finalize_article(report_body, sources)

    stats = {
        "queries": len(queries),
        "candidate_urls": len(candidate_urls),
        "fetched": len(sources),
        "fetch_errors": fetch_errors,
    }
    return FlatResult(
        article=article,
        sources=sources,
        queries=queries,
        warnings=warnings,
        stats=stats,
    )


__all__ = [
    "FlatSource",
    "FlatResult",
    "build_sources_block",
    "finalize_article",
    "run_flat_research",
]
