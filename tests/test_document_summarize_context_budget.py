from __future__ import annotations

import unittest
from types import SimpleNamespace

from core.prompts import BATCH_SUMMARY_PROMPT, DOC_SUMMARY_PROMPT
from tools.document_summarize_tool.document_summarize_tool import DocumentSummarizeTool


class _FakeStore:
    def load_request(self) -> str:
        return "test request"


class _TokenCountingLLM:
    def __init__(self, *, n_ctx: int) -> None:
        self.n_ctx = n_ctx
        self.stream_summary = False
        self.last_user_prompt = ""

    def tokenize_count(self, text: str, *, timeout_sec: float = 0.5) -> int:
        return len(text or "")

    def ask_json(self, _system_prompt, user_prompt, **_kwargs):
        self.last_user_prompt = user_prompt
        return {
            "title": "Doc",
            "source_type": "test",
            "summary": "ok",
            "key_points": [],
            "reliability_notes": [],
            "keywords": [],
            "evidence": [],
        }

    def ask(self, _system_prompt, user_prompt, **_kwargs) -> str:
        self.last_user_prompt = user_prompt
        return "ok"


def _tool(llm) -> DocumentSummarizeTool:
    return DocumentSummarizeTool(schema={}, llm=llm, run_store_service=_FakeStore())


def _record() -> SimpleNamespace:
    return SimpleNamespace(
        doc_id="001",
        title="Doc",
        url="https://example.com",
        final_url="https://example.com",
        domain="example.com",
    )


class DocumentSummarizeContextBudgetTests(unittest.TestCase):
    def test_budget_does_not_use_legacy_floor_when_n_ctx_is_small(self) -> None:
        tool = _tool(SimpleNamespace(n_ctx=8192))

        self.assertEqual(tool._single_pass_budget(), 6656)
        self.assertLess(tool._single_pass_budget(), 16384)

    def test_single_pass_prompt_is_trimmed_to_tokenizer_context(self) -> None:
        llm = _TokenCountingLLM(n_ctx=5000)
        tool = _tool(llm)

        tool._summarize_single_pass(_record(), "x" * 10_000, budget=10_000)

        prompt = "\n".join(
            [
                DOC_SUMMARY_PROMPT.strip(),
                "Return a strict JSON object only.",
                "/no_think",
                llm.last_user_prompt,
            ]
        )
        self.assertLessEqual(
            llm.tokenize_count(prompt),
            llm.n_ctx - tool._CONTEXT_TOKEN_HEADROOM,
        )
        self.assertLess(len(llm.last_user_prompt), 10_000)

    def test_batch_prompt_is_trimmed_to_tokenizer_context(self) -> None:
        # tokenize_count == char count here, so n_ctx is in *chars*: it must
        # exceed the fixed BATCH_SUMMARY_PROMPT system text (~4.8k chars) with
        # room for the doc bodies, while still being far below the 16k chars of
        # documents below so trimming is genuinely exercised. (A real model's
        # n_ctx is in tokens and ~3x larger in char terms, so the system prompt
        # is never the binding constraint there.)
        llm = _TokenCountingLLM(n_ctx=6000)
        tool = _tool(llm)
        records = [_record(), _record()]
        records[1].doc_id = "002"

        prompt_input = tool._fit_batch_prompt_input(records, ["x" * 8000, "y" * 8000])
        prompt = "\n".join(
            [
                BATCH_SUMMARY_PROMPT.strip(),
                "Return a strict JSON object only.",
                "/no_think",
                prompt_input,
            ]
        )

        self.assertLessEqual(
            llm.tokenize_count(prompt),
            llm.n_ctx - tool._CONTEXT_TOKEN_HEADROOM,
        )
        self.assertIn("=== doc_001 ===", prompt_input)
        self.assertIn("=== doc_002 ===", prompt_input)


if __name__ == "__main__":
    unittest.main()
