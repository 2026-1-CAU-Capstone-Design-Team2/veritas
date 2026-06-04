from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.report_markdown_normalizer import normalize_final_report_markdown
from services.run_store_tool_funcs import RunStoreService
from tools.final_report_tool.final_report_tool import (
    FinalReportTool,
    render_final_report_input,
    repair_user_request_section_if_leaked,
)
from tools.loader import TOOLS_DIR, load_schema


_PLAN = {
    "topic": "다중 팔 밴딧 알고리즘",
    "goal": "regret bound 수식 비교",
    "search_queries": ["SECRET_QUERY_ONE", "SECRET_QUERY_TWO"],
    "must_cover": ["ETC 진행 순서", "UCB regret bound"],
    "keywords": ["UCB", "Thompson Sampling"],
}


# What a model that leaked the internal JSON into the report looks like.
_LEAKED_REPORT = (
    "# Final Research Brief\n\n"
    "## User Request\n"
    "{\n"
    '  "user_request": "원래 사용자 요청",\n'
    '  "plan": { "topic": "t", "search_queries": ["SECRET_QUERY_ONE"] },\n'
    '  "batch_summaries": ["배치 내용"]\n'
    "}\n\n"
    "## Executive Summary\n"
    "핵심 요약 본문입니다 [doc_000].\n\n"
    "## Source Notes\n"
    "- doc_1 | RLVR | 2025 | 기여 | High\n"
)


class RenderFinalReportInputTests(unittest.TestCase):
    def test_input_contains_no_raw_json_payload(self) -> None:
        out = render_final_report_input("원래 요청", _PLAN, 3, 1, ["배치 요약 본문"])
        self.assertNotIn('"batch_summaries": [', out)
        self.assertNotIn('"plan": {', out)
        self.assertNotIn('"search_queries"', out)
        self.assertNotIn("{", out)  # no JSON object at all

    def test_input_omits_search_queries_and_keeps_allowlist(self) -> None:
        out = render_final_report_input("원래 요청", _PLAN, 3, 1, ["배치 요약 본문"])
        self.assertNotIn("SECRET_QUERY", out)            # search queries dropped
        self.assertIn("원래 요청", out)
        self.assertIn("다중 팔 밴딧 알고리즘", out)        # topic (allowlisted)
        self.assertIn("regret bound 수식 비교", out)      # goal (allowlisted)
        self.assertIn("배치 요약 본문", out)              # batch summary body

    def test_non_dict_plan_does_not_crash(self) -> None:
        out = render_final_report_input("요청", None, 0, 0, [])
        self.assertIn("요청", out)


class LeakageGuardTests(unittest.TestCase):
    def test_repairs_leaked_user_request_section(self) -> None:
        repaired, was = repair_user_request_section_if_leaked(
            _LEAKED_REPORT, "원래 사용자 요청"
        )
        self.assertTrue(was)
        ur = _user_request_body(repaired)
        self.assertIn("원래 사용자 요청", ur)
        self.assertNotIn('"plan"', ur)
        self.assertNotIn('"batch_summaries"', ur)
        self.assertNotIn("SECRET_QUERY", ur)
        self.assertNotIn("{", ur)
        # Other sections untouched.
        self.assertIn("핵심 요약 본문입니다 [doc_000].", repaired)

    def test_clean_section_is_left_unchanged(self) -> None:
        clean = (
            "# Final Research Brief\n\n"
            "## User Request\n\n> 원래 요청입니다\n\n"
            "## Executive Summary\n본문\n"
        )
        repaired, was = repair_user_request_section_if_leaked(clean, "원래 요청입니다")
        self.assertFalse(was)
        self.assertEqual(repaired, clean)

    def test_no_user_request_section_is_noop(self) -> None:
        md = "# Report\n\n## Summary\n본문\n"
        repaired, was = repair_user_request_section_if_leaked(md, "요청")
        self.assertFalse(was)
        self.assertEqual(repaired, md)

    def test_guard_and_source_notes_normalizer_do_not_interfere(self) -> None:
        repaired, was = repair_user_request_section_if_leaked(
            _LEAKED_REPORT, "원래 사용자 요청"
        )
        self.assertTrue(was)
        normalized = normalize_final_report_markdown(repaired)
        # User Request repaired...
        self.assertIn("원래 사용자 요청", _user_request_body(normalized))
        self.assertNotIn('"batch_summaries"', normalized)
        # ...and the Source Notes bullet row became a canonical table row.
        self.assertIn("| [doc_001] | RLVR | 2025 | 기여 | High |", normalized)


class _LeakingLLM:
    """Returns a report that dumped the internal JSON into ## User Request."""

    n_ctx = 50_000

    def ask(self, system_prompt, user_prompt, **kwargs):
        return _LEAKED_REPORT


class FinalReportToolGuardTests(unittest.TestCase):
    def test_saved_report_has_no_leaked_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStoreService(Path(tmp))
            store.save_request("원래 사용자 요청")
            store.paths.plan_path.write_text(
                json.dumps(_PLAN, ensure_ascii=False), encoding="utf-8"
            )
            store.paths.index_path.write_text(
                json.dumps({"records": [{"doc_id": "000", "duplicate_of": None}]}),
                encoding="utf-8",
            )
            (store.paths.summary_dir / "batch_001.md").write_text(
                "# Batch Summary\n- finding [doc_000]\n", encoding="utf-8"
            )
            tool = FinalReportTool(
                schema=load_schema(TOOLS_DIR / "final_report_tool" / "tool_schema.json"),
                llm=_LeakingLLM(),
                run_store_service=store,
            )

            result = tool.run()

            self.assertTrue(result.success)
            final_md = store.paths.final_path.read_text(encoding="utf-8")
            # The leaked JSON payload is gone; the original request stands in.
            self.assertNotIn('"batch_summaries"', final_md)
            self.assertNotIn('"search_queries"', final_md)
            self.assertIn("원래 사용자 요청", _user_request_body(final_md))


def _user_request_body(markdown: str) -> str:
    lines = markdown.split("\n")
    start = next(i for i, l in enumerate(lines) if l.strip().lower() == "## user request")
    out: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("#"):
            break
        out.append(line)
    return "\n".join(out)


if __name__ == "__main__":
    unittest.main()
