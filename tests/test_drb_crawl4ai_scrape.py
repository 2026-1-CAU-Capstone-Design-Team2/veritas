from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.drb import crawl4ai_scrape as cs


def _ok_fetch(url: str, max_chars: int) -> dict:
    return {"success": True, "title": f"Title of {url}", "text": f"Body content for {url}.", "final_url": url}


def _fail_fetch(url: str, max_chars: int) -> dict:
    return {"success": False, "error": "boom"}


def _record(rid, urls_with_content: dict[str, str | None]) -> dict:
    deduped = {}
    for url, content in urls_with_content.items():
        entry = {"facts": [f"fact about {url}"]}
        if content is not None:
            entry["url_content"] = content
        deduped[url] = entry
    return {"id": rid, "citations": {"x": 1}, "citations_deduped": deduped}


class BuildUrlContentTests(unittest.TestCase):
    def test_success_joins_title_and_content(self) -> None:
        out = cs.build_url_content({"success": True, "title": "T", "text": "Body."})
        self.assertEqual(out, "T\n\nBody.")

    def test_failure_uses_sentinel(self) -> None:
        out = cs.build_url_content({"success": False, "error": "timeout"})
        self.assertTrue(out.startswith("scrape failed:"))
        self.assertIn("timeout", out)

    def test_empty_text_is_failure(self) -> None:
        out = cs.build_url_content({"success": True, "title": "T", "text": "   "})
        self.assertTrue(out.startswith("scrape failed:"))


class ScrapeUrlContentTests(unittest.TestCase):
    def test_retries_then_fails(self) -> None:
        calls = {"n": 0}

        def flaky(url, mc):
            calls["n"] += 1
            return {"success": False, "error": "x"}

        out = cs.scrape_url_content("http://e", flaky, retries=3, retry_sleep=0)
        self.assertEqual(calls["n"], 3)
        self.assertTrue(out.startswith("scrape failed:"))

    def test_stops_on_first_success(self) -> None:
        calls = {"n": 0}

        def once(url, mc):
            calls["n"] += 1
            return {"success": True, "title": "T", "text": "ok"}

        out = cs.scrape_url_content("http://e", once, retries=3, retry_sleep=0)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(out, "T\n\nok")


class FillRecordTests(unittest.TestCase):
    def test_only_missing_urls_are_scraped(self) -> None:
        rec = _record(1, {"http://a": None, "http://b": "already there"})
        cs.fill_record(rec, _ok_fetch, retry_sleep=0)
        self.assertEqual(rec["citations_deduped"]["http://a"]["url_content"], "Title of http://a\n\nBody content for http://a.")
        # Pre-filled content is left untouched.
        self.assertEqual(rec["citations_deduped"]["http://b"]["url_content"], "already there")
        # Facts preserved.
        self.assertEqual(rec["citations_deduped"]["http://a"]["facts"], ["fact about http://a"])

    def test_needed_urls_filter(self) -> None:
        rec = _record(1, {"http://a": None, "http://b": "", "http://c": "have"})
        self.assertEqual(set(cs.needed_urls(rec)), {"http://a", "http://b"})

    def test_failure_sets_sentinel(self) -> None:
        rec = _record(1, {"http://a": None})
        cs.fill_record(rec, _fail_fetch, retries=1, retry_sleep=0)
        self.assertTrue(rec["citations_deduped"]["http://a"]["url_content"].startswith("scrape failed:"))


class ProcessFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.raw = self.root / "deduplicated.jsonl"
        self.out = self.root / "scraped.jsonl"
        rows = [_record(1, {"http://a": None}), _record(2, {"http://b": None})]
        self.raw.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_writes_scraped_with_url_content(self) -> None:
        n = cs.process_file(self.raw, self.out, _ok_fetch, retry_sleep=0)
        self.assertEqual(n, 2)
        rows = list(cs.iter_json_objects(self.out.read_text(encoding="utf-8")))
        self.assertEqual({r["id"] for r in rows}, {1, 2})
        for r in rows:
            for v in r["citations_deduped"].values():
                self.assertTrue(v["url_content"])

    def test_resume_skips_completed_ids(self) -> None:
        cs.process_file(self.raw, self.out, _ok_fetch, retry_sleep=0)
        # Second run should process nothing new (all ids already in output).
        n = cs.process_file(self.raw, self.out, _fail_fetch, retry_sleep=0)
        self.assertEqual(n, 0)
        rows = list(cs.iter_json_objects(self.out.read_text(encoding="utf-8")))
        self.assertEqual(len(rows), 2)  # not duplicated


if __name__ == "__main__":
    unittest.main()
