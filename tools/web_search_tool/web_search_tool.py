from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup

from tools.tool import BaseTool, ToolResult


@dataclass
class SearchResult:
    title: str
    link: str
    snippet: str


class DuckDuckGoSearchProvider:
    """Small, dependency-light DuckDuckGo HTML search provider.

    This intentionally avoids provider orchestration, Docker auto-start,
    public instance probing, and query rewriting. The tool receives a query, normalizes whitespace, sends it
    to DuckDuckGo's HTML endpoint, and returns parsed organic results.
    """

    SEARCH_URL = "https://duckduckgo.com/html/"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )

    def __init__(self, *, timeout_sec: int = 15) -> None:
        self.timeout_sec = max(1, int(timeout_sec))
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ko,en-US;q=0.9,en;q=0.8",
            }
        )

    def search(self, *, query: str, num_results: int) -> list[SearchResult]:
        response = self.session.get(
            self.SEARCH_URL,
            params={"q": query},
            timeout=self.timeout_sec,
        )
        response.raise_for_status()
        return self._parse_results(response.text, limit=num_results)

    def _parse_results(self, html: str, *, limit: int) -> list[SearchResult]:
        soup = BeautifulSoup(html or "", "html.parser")
        results: list[SearchResult] = []
        seen: set[str] = set()

        for node in soup.select(".result"):
            title_node = node.select_one("a.result__a") or node.select_one("a[href]")
            if not title_node:
                continue

            title = title_node.get_text(" ", strip=True)
            link = self._clean_ddg_url(title_node.get("href") or "")
            if not title or not link or not link.startswith(("http://", "https://")):
                continue

            key = self._canonical_key(link)
            if key in seen:
                continue
            seen.add(key)

            snippet_node = node.select_one(".result__snippet") or node.select_one(".result__body")
            snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""

            results.append(SearchResult(title=title, link=link, snippet=snippet))
            if len(results) >= limit:
                break

        return results

    def _clean_ddg_url(self, raw_url: str) -> str:
        raw_url = (raw_url or "").strip()
        if not raw_url:
            return ""

        # DuckDuckGo often wraps result URLs as /l/?uddg=<encoded-url>.
        if raw_url.startswith("//"):
            raw_url = "https:" + raw_url
        if raw_url.startswith("/"):
            raw_url = "https://duckduckgo.com" + raw_url

        parsed = urlparse(raw_url)
        if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
            uddg = parse_qs(parsed.query).get("uddg", [""])[0]
            if uddg:
                return unquote(uddg)

        return raw_url

    def _canonical_key(self, url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc.lower()}{parsed.path}".rstrip("/")


class DDGSPackageSearchProvider:
    """Fallback provider backed by the installed `ddgs` package."""

    def __init__(self, *, timeout_sec: int = 15) -> None:
        self.timeout_sec = max(1, int(timeout_sec))

    def search(self, *, query: str, num_results: int) -> list[SearchResult]:
        try:
            from ddgs import DDGS
        except ImportError as e:
            raise RuntimeError("ddgs package is not installed") from e

        try:
            ddgs_client = DDGS(timeout=self.timeout_sec)
        except TypeError:
            ddgs_client = DDGS()

        if hasattr(ddgs_client, "__enter__"):
            with ddgs_client as ddgs:
                raw_results = self._text_results(ddgs, query=query, num_results=num_results)
        else:
            try:
                raw_results = self._text_results(
                    ddgs_client,
                    query=query,
                    num_results=num_results,
                )
            finally:
                close = getattr(ddgs_client, "close", None)
                if callable(close):
                    close()

        results: list[SearchResult] = []
        seen: set[str] = set()
        for item in raw_results:
            if not isinstance(item, dict):
                continue

            link = str(item.get("href") or item.get("url") or item.get("link") or "").strip()
            title = str(item.get("title") or "").strip()
            snippet = str(item.get("body") or item.get("snippet") or item.get("description") or "").strip()
            if not title or not link or not link.startswith(("http://", "https://")):
                continue

            key = self._canonical_key(link)
            if key in seen:
                continue
            seen.add(key)
            results.append(SearchResult(title=title, link=link, snippet=snippet))
            if len(results) >= num_results:
                break

        return results

    def _text_results(self, ddgs: Any, *, query: str, num_results: int) -> list[dict[str, Any]]:
        return list(
            ddgs.text(
                query,
                region="wt-wt",
                safesearch="moderate",
                max_results=num_results,
            )
        )

    def _canonical_key(self, url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc.lower()}{parsed.path}".rstrip("/")


class WebSearchTool(BaseTool):
    """Search the web using DuckDuckGo HTML search with a ddgs fallback."""

    def __init__(self, schema: dict[str, Any]) -> None:
        super().__init__(schema=schema)

    @property
    def name(self) -> str:
        return "web_search"

    def run(self, query: str, num_results: int = 5) -> ToolResult:
        search_query = " ".join(str(query or "").split()).strip()
        if not search_query:
            return ToolResult(success=False, error="`query` must be a non-empty string.")

        limit = max(1, min(int(num_results or 5), 20))
        provider_errors: list[str] = []
        completed_provider_count = 0

        try:
            results = DuckDuckGoSearchProvider().search(query=search_query, num_results=limit)
            completed_provider_count += 1
        except Exception as e:
            provider_errors.append(f"html: {e}")
            results = []

        if not results:
            try:
                results = DDGSPackageSearchProvider().search(
                    query=search_query,
                    num_results=limit,
                )
                completed_provider_count += 1
            except Exception as e:
                provider_errors.append(f"ddgs: {e}")

        if provider_errors and not results and completed_provider_count == 0:
            return ToolResult(
                success=False,
                error=(
                    f"DuckDuckGo search failed for query={search_query!r}: "
                    + "; ".join(provider_errors)
                ),
            )

        if not results:
            return ToolResult(
                success=True,
                content="",
                data={
                    "query": search_query,
                    "provider": "duckduckgo",
                    "results": [],
                    "warnings": [
                        "DuckDuckGo returned no parseable results; treating as an empty result set.",
                        *provider_errors,
                    ],
                },
            )

        content = "\n".join(
            f"{idx}. {item.title}\n   {item.link}\n   {item.snippet}"
            for idx, item in enumerate(results, start=1)
        )
        return ToolResult(
            success=True,
            content=content,
            data={
                "query": search_query,
                "provider": "duckduckgo",
                "results": [asdict(item) for item in results],
            },
        )
