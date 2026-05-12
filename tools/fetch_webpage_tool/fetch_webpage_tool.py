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
    """Fetch a webpage and extract LLM-friendly plain text.

    This intentionally uses the stable requests + BeautifulSoup path only.
    Browser-based crawlers were removed because they introduced Playwright /
    asyncio cleanup failures on Windows and complicated the fetch pipeline.
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
            title = _clean_text_for_storage(title, max_chars=500)

            body = soup.body if soup.body else soup
            fetch_funcs._strip_noise_tags(body)

            main_node = fetch_funcs._select_main_content_node(body)
            text = fetch_funcs._extract_meaningful_text(main_node, max_chars=max_chars)
            text = _clean_text_for_storage(text, max_chars=max_chars)
            html = _clean_text_for_storage(str(main_node), max_chars=max_chars)

            if len(text.strip()) < 100:
                return ToolResult(success=False, error="extracted too little text")

            final_url = response.url
            domain = urlparse(final_url).netloc.lower()

            return ToolResult(
                success=True,
                content=f"Fetched webpage: {final_url}",
                data=FetchedDocument(
                    title=title,
                    url=fetch_url,
                    final_url=final_url,
                    domain=domain,
                    text=text,
                    html=html,
                    content_type=content_type,
                ),
            )
        except UnicodeError as e:
            return ToolResult(success=False, error=f"encoding error while fetching webpage: {e}")
        except Exception as e:
            return ToolResult(success=False, error=f"failed to fetch webpage: {e}")
