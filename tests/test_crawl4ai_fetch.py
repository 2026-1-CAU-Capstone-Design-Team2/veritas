from __future__ import annotations

import unittest

from services.fetch_webpage_tool_funcs.crawl4ai_fetch import (
    _FIT_MIN_CHARS,
    _FIT_MIN_RATIO,
    _coerce_markdown,
)


class _FakeMarkdown:
    def __init__(self, raw: str, fit: str) -> None:
        self.raw_markdown = raw
        self.fit_markdown = fit


class _FakeResult:
    def __init__(self, markdown) -> None:
        self.markdown = markdown


def _result(raw: str, fit: str) -> _FakeResult:
    return _FakeResult(_FakeMarkdown(raw, fit))


class CoerceMarkdownTests(unittest.TestCase):
    def test_clean_fit_above_ratio_floor_is_used(self) -> None:
        # A de-chromed article body that keeps ~30% of raw — above the 0.25
        # floor. This is the case the old 0.45 floor wrongly reverted to raw.
        raw = "b" * 4000
        fit = "a" * 1200  # ratio 0.30, >= _FIT_MIN_CHARS
        self.assertGreater(0.30, _FIT_MIN_RATIO)
        text, variant = _coerce_markdown(_result(raw, fit))
        self.assertEqual(variant, "fit_markdown")
        self.assertEqual(text, fit)

    def test_degenerate_fit_falls_back_to_raw(self) -> None:
        # Over-stripped low-prose page (ratio ~0.05) → keep raw, no body loss.
        raw = "b" * 20000
        fit = "a" * 1000  # ratio 0.05, below floor despite >= _FIT_MIN_CHARS
        text, variant = _coerce_markdown(_result(raw, fit))
        self.assertEqual(variant, "raw_markdown")
        self.assertEqual(text, raw)

    def test_tiny_fit_below_char_floor_falls_back_to_raw(self) -> None:
        raw = "b" * 4000
        fit = "a" * (_FIT_MIN_CHARS - 100)  # high ratio would pass, chars floor fails
        text, variant = _coerce_markdown(_result(raw, fit))
        self.assertEqual(variant, "raw_markdown")
        self.assertEqual(text, raw)

    def test_no_raw_uses_fit(self) -> None:
        fit = "a" * 1200
        text, variant = _coerce_markdown(_result("", fit))
        self.assertEqual(variant, "fit_markdown")
        self.assertEqual(text, fit)

    def test_plain_string_markdown_is_treated_as_raw(self) -> None:
        text, variant = _coerce_markdown(_FakeResult("plain raw body" * 100))
        self.assertEqual(variant, "raw_markdown")
        self.assertIn("plain raw body", text)

    def test_ratio_floor_is_conservative_value(self) -> None:
        # Guard the tuned value so a future edit that re-tightens it back toward
        # the old 0.45 (which reverted clean articles to noisy raw) is noticed.
        self.assertLessEqual(_FIT_MIN_RATIO, 0.30)
        self.assertGreaterEqual(_FIT_MIN_RATIO, 0.15)


if __name__ == "__main__":
    unittest.main()
