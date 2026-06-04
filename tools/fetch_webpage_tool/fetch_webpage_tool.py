from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

from tools.tool import BaseTool, ToolResult
import services.fetch_webpage_tool_funcs as fetch_funcs


_MIN_TEXT_CHARS = 100


def _normalize_fetch_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path or ""

    is_arxiv = host == "arxiv.org" or host == "www.arxiv.org" or host.endswith(".arxiv.org")
    if not is_arxiv or not path.startswith("/abs/"):
        return url

    paper_id = path[len("/abs/") :].strip("/")
    if not paper_id:
        return url

    return urlunparse(parsed._replace(path=f"/html/{paper_id}"))


def _clean_text_for_storage(text: str, *, max_chars: int | None = None) -> str:
    """Normalize text so downstream UTF-8 writes/reads do not fail.

    Some pages contain invalid surrogates, mixed encodings, or control characters.
    Keeping the pipeline alive is more important than preserving those bytes.
    """
    value = str(text or "")
    value = value.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
    value = value.replace("\x00", "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    if max_chars is not None:
        value = value[: max(0, int(max_chars))]
    return value


def _default_max_chars() -> int:
    raw = os.getenv("VERITAS_FETCH_MAX_CHARS")
    if raw is None:
        return 25_000
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 25_000


@dataclass
class FetchedDocument:
    title: str
    url: str
    final_url: str
    domain: str
    text: str
    html: str
    content_type: str


class FetchWebpageTool(BaseTool):
    """Fetch a webpage and extract LLM-friendly Markdown via Crawl4AI.

    Crawl4AI's HTTP-only crawler strategy (``AsyncHTTPCrawlerStrategy``, backed
    by aiohttp — no Playwright browser) is the *only* fetch path. HTML is
    converted to clean Markdown by Crawl4AI's ``DefaultMarkdownGenerator`` +
    ``PruningContentFilter``, which preserves document structure (headings,
    lists, tables) and strips boilerplate without a hard-coded selector list.

    There is no fallback extractor by design: a URL that Crawl4AI cannot fetch
    or extract is reported as a failure, and the AutoSurvey collect loop simply
    moves on to the next search result. Consequently every document that *is*
    stored was fetched by Crawl4AI and can be persisted directly as Markdown.

    The HTTP-only strategy is deliberate: an earlier browser-based Crawl4AI
    integration had to run in an isolating subprocess because Playwright left
    asyncio transport-cleanup tasks pending on Windows. The HTTP strategy uses
    no browser, so it runs safely in-process with no subprocess and no per-fetch
    interpreter startup cost.
    """

    def __init__(self, schema: dict[str, Any]) -> None:
        super().__init__(schema=schema)

    @property
    def name(self) -> str:
        return "fetch_webpage"

    def run(
        self,
        url: str,
        timeout_sec: int = 15,
        max_chars: int | None = None,
    ) -> ToolResult:
        url = (url or "").strip()
        if not url:
            return ToolResult(success=False, error="`url` must be a non-empty string.")

        fetch_url = _normalize_fetch_url(url)
        resolved_max_chars = _default_max_chars() if max_chars is None else max_chars
        max_chars = max(1000, int(resolved_max_chars))
        timeout_sec = max(1, int(timeout_sec))

        crawl_result = fetch_funcs.fetch_with_crawl4ai(fetch_url, timeout_sec, max_chars)
        if not crawl_result.get("success"):
            error_text = crawl_result.get("error") or "unknown error"
            print(f"[fetch][crawl4ai][failed] url={fetch_url} ({error_text})")
            return ToolResult(success=False, error=f"failed to fetch webpage: {error_text}")

        built = self._build_result(crawl_result, fetch_url=fetch_url, max_chars=max_chars)
        if not built.success:
            print(f"[fetch][crawl4ai][failed] url={fetch_url} ({built.error})")
            return built

        doc = built.data
        print(
            f"[fetch][crawl4ai][ok] url={fetch_url} "
            f"content_type='{doc.content_type}' "
            f"text_chars={len(doc.text)} html_chars={len(doc.html)}"
        )
        return built

    def _build_result(
        self,
        payload: dict[str, Any],
        *,
        fetch_url: str,
        max_chars: int,
    ) -> ToolResult:
        """Turn a Crawl4AI fetch payload into a ToolResult."""
        title = _clean_text_for_storage(payload.get("title") or "", max_chars=500)
        text = _clean_text_for_storage(payload.get("text") or "", max_chars=max_chars)
        # Raw HTML is stored complete as archival provenance. It does not feed a
        # lossy text-extraction step (Crawl4AI already produced the Markdown
        # `text`), so it is intentionally NOT truncated -- this keeps the stored
        # HTML faithful to the original page for later re-processing.
        html = _clean_text_for_storage(payload.get("html") or "")

        if len(text.strip()) < _MIN_TEXT_CHARS:
            return ToolResult(success=False, error="extracted too little text")

        final_url = payload.get("final_url") or fetch_url
        domain = (payload.get("domain") or urlparse(final_url).netloc or "").lower()

        return ToolResult(
            success=True,
            content=f"Fetched webpage: {final_url}",
            data=FetchedDocument(
                title=title,
                url=payload.get("url") or fetch_url,
                final_url=final_url,
                domain=domain,
                text=text,
                html=html,
                content_type=payload.get("content_type") or "",
            ),
        )
