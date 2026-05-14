from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from tools.tool import BaseTool, ToolResult
import services.fetch_webpage_tool_funcs as fetch_funcs


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

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
    """Fetch a webpage and extract LLM-friendly content.

    Primary path: Crawl4AI's HTTP-only crawler strategy (``AsyncHTTPCrawlerStrategy``,
    backed by aiohttp — no Playwright browser). HTML is converted to clean Markdown
    by Crawl4AI's ``DefaultMarkdownGenerator`` + ``PruningContentFilter``, which
    preserves document structure (headings, lists, tables) far better than the
    legacy hand-written extractor and strips boilerplate without a hard-coded
    selector list.

    Fallback path: requests + BeautifulSoup heuristic extraction. Used when
    ``crawl4ai`` is not installed, or when the Crawl4AI fetch fails for a specific
    URL. This keeps the pipeline working even before ``pip install crawl4ai``.

    The HTTP-only strategy is deliberate: an earlier browser-based Crawl4AI
    integration had to run in an isolating subprocess because Playwright left
    asyncio transport-cleanup tasks pending on Windows. The HTTP strategy uses no
    browser, so it runs safely in-process with no subprocess and no per-fetch
    interpreter startup cost.
    """

    def __init__(self, schema: dict[str, Any]) -> None:
        super().__init__(schema=schema)

    @property
    def name(self) -> str:
        return "fetch_webpage"

    def run(self, url: str, timeout_sec: int = 15, max_chars: int = 25000) -> ToolResult:
        url = (url or "").strip()
        if not url:
            return ToolResult(success=False, error="`url` must be a non-empty string.")

        fetch_url = _normalize_fetch_url(url)
        max_chars = max(1000, int(max_chars))
        timeout_sec = max(1, int(timeout_sec))

        errors: list[str] = []

        # Primary path: Crawl4AI HTTP-only strategy. Returns None when crawl4ai
        # is not installed, in which case we go straight to the fallback.
        crawl_result = fetch_funcs.fetch_with_crawl4ai(fetch_url, timeout_sec, max_chars)
        if crawl_result is None:
            print("[fetch][crawl4ai] unavailable (not installed) -> requests+bs4 fallback")
        elif crawl_result.get("success"):
            built = self._build_result(crawl_result, fetch_url=fetch_url, max_chars=max_chars)
            if built.success:
                doc = built.data
                print(
                    f"[fetch][crawl4ai] ok url={fetch_url} "
                    f"content_type='{doc.content_type}' "
                    f"text_chars={len(doc.text)} html_chars={len(doc.html)}"
                )
                return built
            errors.append(f"crawl4ai: {built.error or 'unusable extraction'}")
            print(f"[fetch][crawl4ai] unusable extraction -> fallback ({built.error})")
        else:
            error_text = crawl_result.get("error") or "unknown error"
            errors.append(f"crawl4ai: {error_text}")
            print(f"[fetch][crawl4ai] failed -> fallback ({error_text})")

        # Fallback path: requests + BeautifulSoup.
        fallback = self._fetch_with_requests(
            fetch_url, timeout_sec=timeout_sec, max_chars=max_chars
        )
        if fallback.get("success"):
            built = self._build_result(fallback, fetch_url=fetch_url, max_chars=max_chars)
            if built.success:
                doc = built.data
                print(
                    f"[fetch][bs4-fallback] ok url={fetch_url} "
                    f"text_chars={len(doc.text)} html_chars={len(doc.html)}"
                )
                return built
            errors.append(f"requests/bs4: {built.error or 'unusable extraction'}")
        else:
            errors.append(f"requests/bs4: {fallback.get('error') or 'unknown error'}")

        print(f"[fetch][failed] url={fetch_url} ({'; '.join(errors)})")
        return ToolResult(
            success=False,
            error="failed to fetch webpage (" + "; ".join(errors) + ")",
        )

    def _build_result(
        self,
        payload: dict[str, Any],
        *,
        fetch_url: str,
        max_chars: int,
    ) -> ToolResult:
        """Turn a fetch payload (from either path) into a ToolResult."""
        title = _clean_text_for_storage(payload.get("title") or "", max_chars=500)
        text = _clean_text_for_storage(payload.get("text") or "", max_chars=max_chars)
        # Raw HTML is stored complete as archival provenance. It no longer feeds
        # a lossy text-extraction step, so it is intentionally NOT truncated --
        # `text` is the capped, summarizer-facing extraction. This keeps the
        # stored HTML faithful to the original page for later re-processing.
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

    def _fetch_with_requests(
        self,
        fetch_url: str,
        *,
        timeout_sec: int,
        max_chars: int,
    ) -> dict[str, Any]:
        """Legacy requests + BeautifulSoup extraction, used as a fallback."""
        headers = {"User-Agent": USER_AGENT}
        try:
            response = requests.get(
                fetch_url,
                headers=headers,
                timeout=timeout_sec,
                allow_redirects=True,
            )
            response.raise_for_status()

            # If the server did not provide a charset, requests may default to
            # ISO-8859-1 for text/* and mangle CJK pages. apparent_encoding is
            # not perfect, but it is safer for downstream summarization.
            content_type = response.headers.get("Content-Type", "")
            if not response.encoding or response.encoding.lower() in {"iso-8859-1", "latin-1"}:
                guessed = getattr(response, "apparent_encoding", None)
                if guessed:
                    response.encoding = guessed

            raw_html = _clean_text_for_storage(response.text or "", max_chars=max_chars * 2)
            soup = BeautifulSoup(raw_html, "html.parser")

            title = soup.title.string.strip() if soup.title and soup.title.string else ""

            body = soup.body if soup.body else soup
            fetch_funcs._strip_noise_tags(body)

            main_node = fetch_funcs._select_main_content_node(body)
            text = fetch_funcs._extract_meaningful_text(main_node, max_chars=max_chars)
            html = str(main_node)

            final_url = response.url
            return {
                "success": True,
                "title": title,
                "url": fetch_url,
                "final_url": final_url,
                "domain": urlparse(final_url).netloc.lower(),
                "text": text,
                "html": html,
                "content_type": content_type,
            }
        except UnicodeError as e:
            return {"success": False, "error": f"encoding error while fetching webpage: {e}"}
        except Exception as e:
            return {"success": False, "error": f"failed to fetch webpage: {e}"}
