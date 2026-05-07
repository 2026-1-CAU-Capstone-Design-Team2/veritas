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
    if not is_arxiv:
        return url

    if not path.startswith("/abs/"):
        return url

    paper_id = path[len("/abs/") :].strip("/")
    if not paper_id:
        return url

    normalized = parsed._replace(path=f"/html/{paper_id}")
    return urlunparse(normalized)


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

        headers = {"User-Agent": USER_AGENT}

        try:
            response = requests.get(fetch_url, headers=headers, timeout=timeout_sec, allow_redirects=True)
            response.raise_for_status()

            raw_html = response.text or ""
            soup = BeautifulSoup(raw_html, "html.parser")

            title = soup.title.string.strip() if soup.title and soup.title.string else ""
            body = soup.body if soup.body else soup
            fetch_funcs._strip_noise_tags(body)

            main_node = fetch_funcs._select_main_content_node(body)
            text = fetch_funcs._extract_meaningful_text(main_node, max_chars=max_chars)
            html = str(main_node)[:max_chars]

            final_url = response.url
            domain = urlparse(final_url).netloc.lower()

            return ToolResult(
                success=True,
                content=f"Fetched: {final_url}",
                data=FetchedDocument(
                    title=title,
                    url=fetch_url,
                    final_url=final_url,
                    domain=domain,
                    text=text,
                    html=html,
                    content_type=response.headers.get("Content-Type", ""),
                ),
            )
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to fetch webpage: {e}")