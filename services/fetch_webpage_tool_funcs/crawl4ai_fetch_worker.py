from __future__ import annotations

"""Subprocess worker for Crawl4AI fetching.

Crawl4AI uses Playwright, which manages browser subprocesses and asyncio
transport objects. On Windows, repeatedly creating and closing those objects in
VERITAS' main process can leave Playwright connection tasks pending when the
loop closes, producing noisy `Event loop is closed` / `Task was destroyed`
messages. Running Crawl4AI in this short-lived worker isolates that cleanup from
main process logging. The parent process reads only stdout JSON and captures
stderr.
"""

import argparse
import asyncio
import json
import sys
from typing import Any
from urllib.parse import urlparse


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)


def _coerce_crawl4ai_markdown(value: Any) -> str:
    if isinstance(value, str):
        return value

    for attr in ("fit_markdown", "raw_markdown", "markdown"):
        text = getattr(value, attr, None)
        if isinstance(text, str) and text.strip():
            return text

    if isinstance(value, dict):
        for key in ("fit_markdown", "raw_markdown", "markdown"):
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                return text

    return str(value or "")


def _normalize_text(text: str) -> str:
    lines = [line.rstrip() for line in str(text or "").replace("\r\n", "\n").splitlines()]
    compact: list[str] = []
    blank = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if not blank:
                compact.append("")
            blank = True
            continue
        compact.append(stripped)
        blank = False
    return "\n".join(compact).strip()


async def _fetch(url: str, timeout_sec: int, max_chars: int) -> dict[str, Any]:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

    browser_config = BrowserConfig(
        headless=True,
        verbose=False,
        user_agent=USER_AGENT,
    )
    run_config = CrawlerRunConfig(
        page_timeout=timeout_sec * 1000,
        word_count_threshold=20,
        exclude_external_links=False,
        remove_overlay_elements=True,
        process_iframes=False,
    )

    async with AsyncWebCrawler(config=browser_config) as crawler:
        result = await crawler.arun(url=url, config=run_config)

    success = bool(getattr(result, "success", True))
    if not success:
        error = getattr(result, "error_message", "") or "unknown Crawl4AI error"
        raise RuntimeError(error)

    final_url = str(getattr(result, "url", "") or url)
    raw_html = str(getattr(result, "html", "") or "")
    metadata = getattr(result, "metadata", {}) or {}
    title = str(metadata.get("title") or "") if isinstance(metadata, dict) else ""
    markdown = _coerce_crawl4ai_markdown(getattr(result, "markdown", ""))
    cleaned_text = _normalize_text(markdown)[:max_chars]

    if len(cleaned_text) < 300:
        raise RuntimeError("Crawl4AI extracted too little text")

    return {
        "success": True,
        "title": title,
        "url": url,
        "final_url": final_url,
        "domain": urlparse(final_url).netloc.lower(),
        "text": cleaned_text,
        "html": raw_html[:max_chars],
        "content_type": "text/markdown; extraction=crawl4ai",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--timeout-sec", type=int, default=15)
    parser.add_argument("--max-chars", type=int, default=25000)
    args = parser.parse_args()

    try:
        payload = asyncio.run(_fetch(args.url, max(1, args.timeout_sec), max(1000, args.max_chars)))
    except Exception as e:
        payload = {"success": False, "error": str(e)}

    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
