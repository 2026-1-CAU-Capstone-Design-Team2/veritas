"""ActiveAnchor extraction + confidence-band tests."""
from __future__ import annotations

import unittest

from services.proactive.anchors import (
    MIN_CONFIDENCE_FOR_ACTIVE_SUGGESTION,
    ActiveAnchor,
    compute_anchor_id,
    confidence_from_source,
)


class ConfidenceBandTests(unittest.TestCase):
    def test_native_cursor_is_high_confidence(self) -> None:
        c = confidence_from_source(
            source="native_cursor", has_cursor=True, has_paragraph=True, has_section=True
        )
        self.assertGreaterEqual(c, 0.90)
        self.assertLessEqual(c, 1.0)

    def test_uia_caret_is_mid_high_confidence(self) -> None:
        c = confidence_from_source(
            source="uia_caret", has_cursor=True, has_paragraph=True, has_section=False
        )
        self.assertGreaterEqual(c, 0.75)
        self.assertLessEqual(c, 0.95)

    def test_ocr_only_is_low_confidence(self) -> None:
        c = confidence_from_source(
            source="ocr_visible_text", has_cursor=False, has_paragraph=True, has_section=False
        )
        # OCR with paragraph approximation: at best mid-band.
        self.assertLess(c, 0.70)

    def test_unknown_is_zero(self) -> None:
        c = confidence_from_source(
            source="unknown", has_cursor=False, has_paragraph=False, has_section=False
        )
        self.assertEqual(c, 0.0)


class AnchorIdStabilityTests(unittest.TestCase):
    def test_same_position_same_paragraph_yields_same_id(self) -> None:
        a = ActiveAnchor(
            document_id="doc-1",
            surface="native_editor",
            cursor_index=120,
            paragraph_text="이것은 동일한 단락입니다. 더 많은 내용이 있습니다.",
            source="native_cursor",
            confidence=0.95,
        )
        b = ActiveAnchor(
            document_id="doc-1",
            surface="native_editor",
            cursor_index=125,  # within same 80-char bucket
            paragraph_text="이것은 동일한 단락입니다. 더 많은 내용이 있습니다.",
            source="native_cursor",
            confidence=0.95,
        )
        self.assertEqual(a.anchor_id, b.anchor_id)

    def test_different_paragraph_yields_different_id(self) -> None:
        a = ActiveAnchor(
            document_id="doc-1",
            surface="native_editor",
            cursor_index=120,
            paragraph_text="첫 단락",
            source="native_cursor",
            confidence=0.95,
        )
        b = ActiveAnchor(
            document_id="doc-1",
            surface="native_editor",
            cursor_index=120,
            paragraph_text="다른 단락",
            source="native_cursor",
            confidence=0.95,
        )
        self.assertNotEqual(a.anchor_id, b.anchor_id)


class ActiveSuggestionCapableTests(unittest.TestCase):
    def test_low_confidence_anchor_not_capable(self) -> None:
        a = ActiveAnchor(
            document_id="doc-x",
            surface="external_app",
            paragraph_text="some captured text",
            source="ocr_visible_text",
            confidence=0.30,
        )
        self.assertFalse(a.is_active_suggestion_capable())

    def test_high_confidence_anchor_capable(self) -> None:
        a = ActiveAnchor(
            document_id="doc-x",
            surface="native_editor",
            cursor_index=10,
            paragraph_text="hi",
            source="native_cursor",
            confidence=0.95,
        )
        self.assertTrue(a.is_active_suggestion_capable())
        self.assertGreaterEqual(a.confidence, MIN_CONFIDENCE_FOR_ACTIVE_SUGGESTION)


if __name__ == "__main__":
    unittest.main()
