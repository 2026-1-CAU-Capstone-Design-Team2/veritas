"""Crawl4AI-backed replacement for DRB's FACT scrape stage (no Jina key).

DRB's FACT pipeline is: ``extract → deduplicate → scrape → validate → stat``.
Only the **scrape** stage (``utils/scrape.py``) needs Jina (`JINA_API_KEY`) — it
turns each cited URL into reference text that the validator judges. This module
is a drop-in replacement for that one stage using Veritas's own
``fetch_with_crawl4ai``, so FACT can run with **no Jina key**.

Contract parity with ``utils/scrape.py`` (so ``utils.validate`` is unchanged):

* input  = ``deduplicated.jsonl`` (rows with ``id`` + ``citations_deduped`` =
  ``{url: {"facts": [...], "url_content"?: str}}``),
* output = ``scraped.jsonl``: the same rows with each citation's
  ``url_content`` filled (``"<title>\\n\\n<content>"`` on success, or
  ``"scrape failed: <error>"`` on failure — same sentinel the validator treats
  as an invalid reference),
* only URLs missing ``url_content`` are scraped; completed ``id``s are skipped
  on resume; records are appended one-per-line as they complete.

⚠️ This makes FACT a **non-official, internal** variant (different scraper than
the leaderboard's Jina). Apply the same scraper to both compared systems and
label results ``fact_crawl4ai_budget`` — never an official DRB/leaderboard score.
Crawl4AI is HTTP-only (no JS render), so it is weaker than Jina on JS/anti-bot
pages; the citation URLs in this benchmark were themselves produced by Crawl4AI,
so re-scraping with Crawl4AI stays internally consistent.

The vendored DRB evaluator is left untouched: swap ``python -m utils.scrape``
for ``python -m benchmarks.drb.crawl4ai_scrape`` in the FACT sequence.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

# Allow running this file directly from inside ``deep_research_bench/`` (where the
# other FACT stages run) — put the Veritas repo root on the path so the
# ``services`` / ``benchmarks`` imports below resolve regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import services.fetch_webpage_tool_funcs as fetch_funcs  # noqa: E402
from benchmarks.drb.drb_io import iter_json_objects  # noqa: E402


FetchFn = Callable[[str, int], dict[str, Any]]

_DEFAULT_MAX_CHARS = 20_000
_DEFAULT_TIMEOUT = 25
_DEFAULT_RETRIES = 3
_DEFAULT_CONCURRENCY = 4


def build_url_content(result: dict[str, Any]) -> str:
    """Turn a crawl4ai fetch result into the reference string the validator reads.

    Mirrors ``utils/scrape.py``: ``"<title>\\n\\n<content>"`` on success, or the
    ``"scrape failed: <error>"`` sentinel on failure (which the validator treats
    as an invalid reference → its statements score ``unknown``).
    """
    if result.get("success") and str(result.get("text") or "").strip():
        title = str(result.get("title") or "").strip()
        content = str(result.get("text") or "").strip()
        return f"{title}\n\n{content}".strip()
    error = str(result.get("error") or "no content")
    return f"scrape failed: {error}"


def scrape_url_content(
    url: str,
    fetch_fn: FetchFn,
    *,
    max_chars: int = _DEFAULT_MAX_CHARS,
    retries: int = _DEFAULT_RETRIES,
    retry_sleep: float = 1.0,
) -> str:
    """Fetch one URL (with retries) and return its reference string."""
    result: dict[str, Any] = {}
    for attempt in range(max(1, retries)):
        result = fetch_fn(url, max_chars) or {}
        if result.get("success") and str(result.get("text") or "").strip():
            break
        if retry_sleep > 0 and attempt < retries - 1:
            time.sleep(retry_sleep)
    return build_url_content(result)


def needed_urls(record: dict[str, Any]) -> list[str]:
    """URLs in this record's ``citations_deduped`` that still lack ``url_content``."""
    deduped = record.get("citations_deduped")
    if not isinstance(deduped, dict):
        return []
    return [
        url
        for url, value in deduped.items()
        if not isinstance(value, dict) or not str(value.get("url_content") or "").strip()
    ]


def fill_record(
    record: dict[str, Any],
    fetch_fn: FetchFn,
    *,
    max_chars: int = _DEFAULT_MAX_CHARS,
    retries: int = _DEFAULT_RETRIES,
    retry_sleep: float = 1.0,
    concurrency: int = 1,
) -> dict[str, Any]:
    """Fill ``url_content`` for every citation URL that needs scraping (in place)."""
    deduped = record.get("citations_deduped")
    if not isinstance(deduped, dict):
        return record
    urls = needed_urls(record)
    if not urls:
        return record

    def _one(url: str) -> tuple[str, str]:
        return url, scrape_url_content(
            url, fetch_fn, max_chars=max_chars, retries=retries, retry_sleep=retry_sleep
        )

    if concurrency > 1 and len(urls) > 1:
        with ThreadPoolExecutor(max_workers=min(concurrency, len(urls))) as pool:
            pairs = list(pool.map(_one, urls))
    else:
        pairs = [_one(url) for url in urls]

    for url, content in pairs:
        if isinstance(deduped.get(url), dict):
            deduped[url]["url_content"] = content
    return record


def process_file(
    raw_path: str | Path,
    output_path: str | Path,
    fetch_fn: FetchFn,
    *,
    max_chars: int = _DEFAULT_MAX_CHARS,
    retries: int = _DEFAULT_RETRIES,
    retry_sleep: float = 1.0,
    concurrency: int = 1,
) -> int:
    """Scrape every record's citations and append to ``output_path``; resume-aware.

    Returns the number of records newly processed. Completed ``id``s already in
    ``output_path`` are skipped, and each record is written as it finishes so a
    long run is resumable.
    """
    raw_path = Path(raw_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    records = list(iter_json_objects(raw_path.read_text(encoding="utf-8")))
    done: set[str] = set()
    if output_path.exists():
        done = {
            str(r.get("id"))
            for r in iter_json_objects(output_path.read_text(encoding="utf-8"))
            if "id" in r
        }

    processed = 0
    with output_path.open("a", encoding="utf-8") as handle:
        for record in records:
            if "id" in record and str(record["id"]) in done:
                continue
            fill_record(
                record,
                fetch_fn,
                max_chars=max_chars,
                retries=retries,
                retry_sleep=retry_sleep,
                concurrency=concurrency,
            )
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            processed += 1
    return processed


def _real_fetch_fn(timeout_sec: int) -> FetchFn:
    def fetch_fn(url: str, max_chars: int) -> dict[str, Any]:
        return fetch_funcs.fetch_with_crawl4ai(url, timeout_sec=timeout_sec, max_chars=max_chars)

    return fetch_fn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Crawl4AI-backed FACT scrape stage (drop-in for utils.scrape; no Jina key)."
    )
    parser.add_argument("--raw_data_path", "--raw-data-path", dest="raw_data_path", required=True,
                        help="deduplicated.jsonl from the FACT deduplicate stage.")
    parser.add_argument("--output_path", "--output-path", dest="output_path", required=True,
                        help="scraped.jsonl to write (validate stage reads this).")
    parser.add_argument("--n_total_process", "--n-total-process", "--concurrency",
                        dest="concurrency", type=int, default=_DEFAULT_CONCURRENCY)
    parser.add_argument("--max-chars", dest="max_chars", type=int, default=_DEFAULT_MAX_CHARS)
    parser.add_argument("--timeout", type=int, default=_DEFAULT_TIMEOUT)
    parser.add_argument("--retries", type=int, default=_DEFAULT_RETRIES)
    args = parser.parse_args(argv)

    # Scraped web content is full of non-ASCII; force UTF-8 stdio so a print
    # never dies on a cp949 console (Korean Windows).
    try:
        from core.stdio_utf8 import force_utf8_stdio

        force_utf8_stdio()
    except Exception:  # noqa: BLE001 — best-effort console fix
        pass

    if not fetch_funcs.crawl4ai_available():
        print("[drb][scrape][error] crawl4ai is not installed (pip install crawl4ai)")
        return 1

    print(f"[drb][scrape] crawl4ai scrape: {args.raw_data_path} → {args.output_path} "
          f"(concurrency={args.concurrency}, max_chars={args.max_chars})")
    processed = process_file(
        args.raw_data_path,
        args.output_path,
        _real_fetch_fn(args.timeout),
        max_chars=args.max_chars,
        retries=args.retries,
        concurrency=args.concurrency,
    )
    print(f"[drb][scrape] done — {processed} record(s) scraped → {args.output_path}")
    return 0


__all__ = [
    "build_url_content",
    "scrape_url_content",
    "needed_urls",
    "fill_record",
    "process_file",
]


if __name__ == "__main__":
    raise SystemExit(main())
