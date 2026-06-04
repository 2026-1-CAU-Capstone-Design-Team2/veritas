"""LLM-based working-fact extraction: JSON parse + regex fallback."""

from __future__ import annotations

import unittest

from services.memory_tools_funcs.main_context.llm_fact_extractor import LLMFactExtractor


class _JsonLLM:
    def __init__(self, response):
        self.response = response

    def ask_json(self, system, user, **kwargs):
        return self.response


class _BrokenJsonLLM:
    def ask_json(self, system, user, **kwargs):
        raise RuntimeError("endpoint down")


class _NoAskJsonLLM:
    """No ask_json → extractor must fall back to regex."""


class LLMFactExtractorTests(unittest.TestCase):
    def test_extracts_fact_and_category(self) -> None:
        ex = LLMFactExtractor(_JsonLLM({"fact": "보수적 투자 성향", "category": "preference"}))
        self.assertEqual(ex.extract("저는 보수적으로 투자해요"), [("preference", "보수적 투자 성향")])

    def test_null_fact_returns_empty(self) -> None:
        ex = LLMFactExtractor(_JsonLLM({"fact": None}))
        self.assertEqual(ex.extract("오늘 날씨 어때?"), [])

    def test_invalid_category_becomes_remember(self) -> None:
        ex = LLMFactExtractor(_JsonLLM({"fact": "등산 좋아함", "category": "weird"}))
        self.assertEqual(ex.extract("등산 좋아해요"), [("remember", "등산 좋아함")])

    def test_fallback_to_regex_on_llm_error(self) -> None:
        ex = LLMFactExtractor(_BrokenJsonLLM())
        self.assertEqual(ex.extract("내 이름은 박서원"), [("name", "박서원")])
        self.assertEqual(ex.extract("오늘 날씨 어때?"), [])

    def test_fallback_to_regex_without_ask_json(self) -> None:
        ex = LLMFactExtractor(_NoAskJsonLLM())
        self.assertEqual(ex.extract("제 이름은 Dana"), [("name", "Dana")])
        self.assertEqual(ex.extract("그 파일 어디 있지?"), [])

    def test_empty_text(self) -> None:
        ex = LLMFactExtractor(_JsonLLM({"fact": "x"}))
        self.assertEqual(ex.extract("   "), [])


if __name__ == "__main__":
    unittest.main()
