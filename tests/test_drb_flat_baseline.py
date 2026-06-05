from __future__ import annotations

import re
import unittest
from pathlib import Path

from benchmarks.drb import flat_agent
from benchmarks.drb.flat_agent import run_flat_research


def _fake_search(query: str, num_results: int) -> list[dict]:
    # Each query yields one unique URL plus one shared URL (to exercise dedupe).
    return [
        {"title": f"T-{query}", "link": f"http://example.com/{query}", "snippet": "s"},
        {"title": "shared", "link": "http://example.com/shared", "snippet": "s"},
    ]


def _fake_fetch(url: str, max_chars: int) -> dict:
    return {
        "success": True,
        "text": ("body text " * 50)[:max_chars],
        "final_url": url,
        "title": "Title",
        "domain": "example.com",
    }


class FlatBaselineTests(unittest.TestCase):
    def test_query_count_is_capped(self) -> None:
        # query_fn returns 12 queries; the agent must cap to search_query_count.
        result = run_flat_research(
            "task",
            language="en",
            query_fn=lambda p, lang, n: [f"q{i}" for i in range(12)],
            search_fn=_fake_search,
            fetch_fn=_fake_fetch,
            report_fn=lambda p, lang, block: "Report [1].",
            max_docs=20,
            search_query_count=5,
        )
        self.assertEqual(len(result.queries), 5)

    def test_fetched_doc_cap_enforced(self) -> None:
        result = run_flat_research(
            "task",
            language="en",
            query_fn=lambda p, lang, n: [f"q{i}" for i in range(5)],
            search_fn=_fake_search,
            fetch_fn=_fake_fetch,
            report_fn=lambda p, lang, block: "Report [1].",
            max_docs=3,
            search_query_count=5,
        )
        self.assertEqual(len(result.sources), 3)

    def test_url_dedupe(self) -> None:
        result = run_flat_research(
            "task",
            language="en",
            query_fn=lambda p, lang, n: ["qa", "qb", "qc"],
            search_fn=_fake_search,
            fetch_fn=_fake_fetch,
            report_fn=lambda p, lang, block: "Report [1].",
            max_docs=50,
            search_query_count=5,
        )
        urls = [s.url for s in result.sources]
        # 3 unique per-query URLs + 1 shared URL, deduped to a single shared one.
        self.assertEqual(urls.count("http://example.com/shared"), 1)
        self.assertEqual(len(urls), len(set(urls)))
        self.assertEqual(len(result.sources), 4)

    def test_report_prompt_gets_numeric_ids_and_urls(self) -> None:
        captured: dict[str, str] = {}

        def fake_report(prompt: str, language: str, block: str) -> str:
            captured["block"] = block
            return "The claim holds [1] and also [2]."

        result = run_flat_research(
            "task",
            language="en",
            query_fn=lambda p, lang, n: ["qa", "qb"],
            search_fn=_fake_search,
            fetch_fn=_fake_fetch,
            report_fn=fake_report,
            max_docs=5,
            search_query_count=5,
        )
        # The source packet handed to the model carries numeric ids and URLs.
        self.assertIn("[1]", captured["block"])
        self.assertRegex(captured["block"], r"https?://")
        # The finalized article keeps inline numeric citations and a URL-bearing
        # References section (deterministically appended, never invented).
        self.assertIn("[1]", result.article)
        self.assertIn("## References", result.article)
        self.assertRegex(result.article, r"\[1\] .+ — https?://")

    def test_model_written_references_are_replaced(self) -> None:
        def report_with_refs(prompt: str, language: str, block: str) -> str:
            return "Body [1].\n\n## References\n[1] hallucinated — http://fake/invented\n"

        result = run_flat_research(
            "task",
            language="en",
            query_fn=lambda p, lang, n: ["qa"],
            search_fn=_fake_search,
            fetch_fn=_fake_fetch,
            report_fn=report_with_refs,
            max_docs=5,
        )
        self.assertNotIn("http://fake/invented", result.article)
        self.assertIn("http://example.com/", result.article)

    def test_empty_query_falls_back_to_task_prompt(self) -> None:
        result = run_flat_research(
            "my task",
            query_fn=lambda p, lang, n: [],
            search_fn=_fake_search,
            fetch_fn=_fake_fetch,
            report_fn=lambda p, lang, block: "Report [1].",
        )
        self.assertEqual(result.queries, ["my task"])
        self.assertTrue(any("fell back" in w for w in result.warnings))

    def test_flat_agent_does_not_import_autosurvey(self) -> None:
        # The control arm must not pull in AutoSurvey orchestration or its tools.
        source = Path(flat_agent.__file__).read_text(encoding="utf-8")
        forbidden = [
            "from workflows",
            "import workflows",
            "AutoSurveyWorkflow",
            "AutoSurveyTool",
            "QueryPlanTool",
            "DocumentCleanupTool",
            "DocumentSummarizeTool",
            "FinalReportTool",
            "from tools",
            "import tools",
        ]
        for token in forbidden:
            self.assertNotIn(token, source, f"flat_agent must not reference {token!r}")


if __name__ == "__main__":
    unittest.main()
