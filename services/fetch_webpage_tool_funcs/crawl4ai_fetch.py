from __future__ import annotations

"""In-process Crawl4AI fetching for the fetch_webpage tool.

This uses Crawl4AI's HTTP-only crawler strategy (``AsyncHTTPCrawlerStrategy``),
which is backed by ``aiohttp`` and does NOT launch a Playwright browser. Because
no browser subprocess or browser-managed asyncio transport is involved, this can
run safely in-process via ``asyncio.run()``.

This replaces the previous ``crawl4ai_fetch_worker.py``, which drove Crawl4AI's
*browser* strategy and therefore had to run in an isolating subprocess to keep
Playwright's noisy asyncio cleanup off the main process. The HTTP-only strategy
removes that requirement entirely: no browser, no subprocess, no per-fetch
interpreter startup cost.

HTML -> Markdown conversion is delegated to Crawl4AI's ``DefaultMarkdownGenerator``
combined with ``PruningContentFilter``. This replaces the hand-written
BeautifulSoup heuristic extractor, which is kept only as an offline fallback in
``fetch_webpage_tool.py`` for environments where ``crawl4ai`` is not installed.
"""

import asyncio
import threading
from typing import Any
from urllib.parse import urlparse


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

# fit_markdown (PruningContentFilter output) is only trusted when it preserves a
# meaningful fraction of raw_markdown. If the filter is too aggressive on a given
# page we keep raw_markdown instead, so the summarizer never silently loses body
# content. This is the "loss-free" safety net.
_FIT_MIN_CHARS = 500
_FIT_MIN_RATIO = 0.45

# Minimum usable extraction length; below this the page is treated as a failure
# so the caller can fall back or skip it.
_MIN_TEXT_CHARS = 100


def crawl4ai_available() -> bool:
    """Return True when the optional ``crawl4ai`` dependency can be imported."""
    try:
        import crawl4ai  # noqa: F401

        return True
    except Exception:
        return False


def _coerce_markdown(result: Any) -> tuple[str, str]:
    """Pick the best markdown variant from a Crawl4AI result.

    Returns ``(markdown_text, variant_label)`` where variant_label is one of
    ``"fit_markdown"`` or ``"raw_markdown"``.
    """
    markdown_obj = getattr(result, "markdown", "") or ""

    raw = getattr(markdown_obj, "raw_markdown", None)
    fit = getattr(markdown_obj, "fit_markdown", None)
    if raw is None and isinstance(markdown_obj, str):
        raw = markdown_obj

    raw = (raw or "").strip()
    fit = (fit or "").strip()

    if fit and len(fit) >= _FIT_MIN_CHARS and (
        not raw or len(fit) >= len(raw) * _FIT_MIN_RATIO
    ):
        return fit, "fit_markdown"
    if raw:
        return raw, "raw_markdown"
    return fit, "fit_markdown"


def _normalize_markdown(text: str) -> str:
    """Trim trailing whitespace and collapse runs of blank lines."""
    lines = [line.rstrip() for line in str(text or "").replace("\r\n", "\n").splitlines()]
    compact: list[str] = []
    blank = False
    for line in lines:
        if not line.strip():
            if not blank:
                compact.append("")
            blank = True
            continue
        compact.append(line)
        blank = False
    return "\n".join(compact).strip()


async def _afetch(url: str, timeout_sec: int, max_chars: int) -> dict[str, Any]:
    from crawl4ai import (
        AsyncWebCrawler,
        BrowserConfig,
        CacheMode,
        CrawlerRunConfig,
        DefaultMarkdownGenerator,
        HTTPCrawlerConfig,
        PruningContentFilter,
    )
    from crawl4ai.async_crawler_strategy import AsyncHTTPCrawlerStrategy

    http_config = HTTPCrawlerConfig(
        method="GET",
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        verify_ssl=True,
    )
    strategy = AsyncHTTPCrawlerStrategy(browser_config=http_config)

    markdown_generator = DefaultMarkdownGenerator(
        content_filter=PruningContentFilter(
            threshold=0.48,
            threshold_type="dynamic",
            min_word_threshold=5,
        )
    )
    run_config = CrawlerRunConfig(
        markdown_generator=markdown_generator,
        word_count_threshold=10,
        # AsyncHTTPCrawlerStrategy reads page_timeout as a seconds value
        # (aiohttp ClientTimeout total), unlike the browser strategy.
        page_timeout=max(1, int(timeout_sec)),
        cache_mode=CacheMode.BYPASS,
        verbose=False,
    )

    async with AsyncWebCrawler(
        crawler_strategy=strategy,
        config=BrowserConfig(verbose=False),
    ) as crawler:
        result = await crawler.arun(url=url, config=run_config)

    if not bool(getattr(result, "success", False)):
        error = getattr(result, "error_message", "") or "unknown Crawl4AI error"
        return {"success": False, "error": str(error)}

    markdown, variant = _coerce_markdown(result)
    text = _normalize_markdown(markdown)[:max_chars]
    if len(text.strip()) < _MIN_TEXT_CHARS:
        return {"success": False, "error": "Crawl4AI extracted too little text"}

    final_url = str(
        getattr(result, "redirected_url", "")
        or getattr(result, "url", "")
        or url
    )
    metadata = getattr(result, "metadata", None) or {}
    title = ""
    if isinstance(metadata, dict):
        title = str(metadata.get("title") or "").strip()

    raw_html = str(getattr(result, "html", "") or "")

    return {
        "success": True,
        "title": title,
        "url": url,
        "final_url": final_url,
        "domain": urlparse(final_url).netloc.lower(),
        "text": text,
        "html": raw_html,
        "content_type": f"text/markdown; extraction=crawl4ai:{variant}",
    }


def fetch_with_crawl4ai(
    url: str,
    timeout_sec: int,
    max_chars: int,
) -> dict[str, Any]:
    """Fetch a page via Crawl4AI's HTTP-only crawler strategy.

    Returns a ``dict`` with ``success=True`` and the extracted markdown on
    success, or ``success=False`` and an ``error`` string on any failure —
    including ``crawl4ai`` not being installed. Crawl4AI is the only fetch
    path: a failed fetch means the document is skipped, not fetched another way.
    """
    if not crawl4ai_available():
        return {
            "success": False,
            "error": "crawl4ai is not installed (pip install crawl4ai)",
        }

    url = (url or "").strip()
    if not url:
        return {"success": False, "error": "`url` must be a non-empty string."}

    timeout_sec = max(1, int(timeout_sec))
    max_chars = max(1000, int(max_chars))
    # Hard outer bound: never let a single fetch stall the workflow even if the
    # crawler's internal timeout misbehaves.
    outer_timeout = timeout_sec + 15

    async def _runner() -> dict[str, Any]:
        try:
            return await asyncio.wait_for(
                _afetch(url, timeout_sec, max_chars),
                timeout=outer_timeout,
            )
        except asyncio.TimeoutError:
            return {
                "success": False,
                "error": f"Crawl4AI fetch timed out after {outer_timeout}s",
            }
        except Exception as e:  # noqa: BLE001 - surface any crawler failure to caller
            return {"success": False, "error": f"Crawl4AI fetch failed: {e}"}

    try:
        return asyncio.run(_runner())
    except RuntimeError:
        # An event loop is already running in this thread (e.g. called from an
        # async context); run the fetch on a private loop in a worker thread.
        box: dict[str, Any] = {}

        def _thread_target() -> None:
            box["result"] = asyncio.run(_runner())

        worker = threading.Thread(target=_thread_target, daemon=True)
        worker.start()
        worker.join()
        return box.get(
            "result",
            {"success": False, "error": "Crawl4AI worker thread produced no result"},
        )
