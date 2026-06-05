from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.drb import citation_adapter as ca


_FINAL_MD = (
    "# Final Research Brief\n\n"
    "## Consolidated Findings\n"
    "PLSM improved on Atari [doc_000]. RLVR-World uses rewards [doc_001][doc_002].\n"
    "Bare cite doc_001 repeats. Inline `doc_000` stays literal. "
    "Link [orig](http://example.com/doc_000) stays.\n"
    "An unmapped source is cited here [doc_009].\n\n"
    "```\n"
    "fenced [doc_000] stays literal\n"
    "```\n\n"
    "## Source Notes\n"
    "| Doc ID | Title |\n"
    "|---|---|\n"
    "| [doc_000] | PLSM |\n"
)


def _index(records: list[dict]) -> dict:
    return {"records": records}


class RenumberCitationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.doc_meta = {
            "000": {"title": "PLSM paper", "url": "http://old/000", "final_url": "http://final/000", "domain": "final"},
            "001": {"title": "RLVR", "url": "http://site/001", "final_url": "", "domain": "site"},
            "002": {"title": "", "url": "", "final_url": "", "domain": "thirdparty"},
        }

    def test_markers_convert_to_first_appearance_numbers(self) -> None:
        article, warnings = ca.export_markdown_to_article(_FINAL_MD, self.doc_meta)
        # doc_000→[1], doc_001→[2], doc_002→[3], doc_009→[4]
        self.assertIn("PLSM improved on Atari [1].", article)
        self.assertIn("rewards [2][3].", article)
        self.assertIn("Bare cite [2] repeats.", article)
        self.assertIn("cited here [4].", article)

    def test_final_url_preferred_over_url(self) -> None:
        article, _ = ca.export_markdown_to_article(_FINAL_MD, self.doc_meta)
        self.assertIn("[1] PLSM paper — http://final/000", article)
        # doc_001 has no final_url → falls back to url
        self.assertIn("[2] RLVR — http://site/001", article)

    def test_unmapped_doc_warns_without_crashing(self) -> None:
        _, warnings = ca.export_markdown_to_article(_FINAL_MD, self.doc_meta)
        self.assertTrue(any("doc_009" in w for w in warnings))

    def test_missing_url_marked_unavailable(self) -> None:
        article, _ = ca.export_markdown_to_article(_FINAL_MD, self.doc_meta)
        self.assertIn("[3]", article)
        self.assertIn("(source URL unavailable)", article)

    def test_code_link_and_inline_are_not_renumbered(self) -> None:
        article, _ = ca.export_markdown_to_article(_FINAL_MD, self.doc_meta)
        self.assertIn("`doc_000` stays literal", article)
        self.assertIn("[orig](http://example.com/doc_000)", article)
        self.assertIn("fenced [doc_000] stays literal", article)

    def test_references_section_present(self) -> None:
        article, _ = ca.export_markdown_to_article(_FINAL_MD, self.doc_meta)
        self.assertIn("## References", article)

    def test_duplicate_urls_handled_deterministically(self) -> None:
        meta = {
            "000": {"title": "A", "final_url": "http://same/x", "url": ""},
            "001": {"title": "B", "final_url": "http://same/x", "url": ""},
        }
        md = "Claim one [doc_000]. Claim two [doc_001].\n"
        first, _ = ca.export_markdown_to_article(md, meta)
        second, _ = ca.export_markdown_to_article(md, meta)
        self.assertEqual(first, second)
        self.assertIn("[1] A — http://same/x", first)
        self.assertIn("[2] B — http://same/x", first)


class WorkspaceExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        (self.ws / "summary").mkdir(parents=True, exist_ok=True)
        self.final_path = self.ws / "final.md"
        self.final_path.write_text(_FINAL_MD, encoding="utf-8")
        (self.ws / "summary" / "index.json").write_text(
            json.dumps(
                _index(
                    [
                        {"doc_id": "000", "title": "PLSM", "url": "http://old/000", "final_url": "http://final/000", "domain": "final"},
                        {"doc_id": "001", "title": "RLVR", "url": "http://site/001", "domain": "site"},
                        {"doc_id": "002", "title": "Third", "domain": "thirdparty"},
                        {"doc_id": "dup_000", "title": "dup", "duplicate_of": "000"},
                    ]
                ),
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_export_reads_index_and_builds_article(self) -> None:
        before = self.final_path.read_text(encoding="utf-8")
        article, warnings = ca.export_workspace_to_article(self.ws)
        self.assertIn("[1]", article)
        self.assertIn("http://final/000", article)
        # final.md must be untouched.
        self.assertEqual(self.final_path.read_text(encoding="utf-8"), before)

    def test_duplicate_record_is_ignored_in_meta(self) -> None:
        meta = ca.load_doc_meta(self.ws / "summary" / "index.json")
        self.assertIn("000", meta)
        self.assertNotIn("dup_000", meta)


if __name__ == "__main__":
    unittest.main()
