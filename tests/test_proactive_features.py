"""Unit tests for ``services.proactive.features``.

After the lexical-keyword purge this module only carries numeric helpers and
the primitive-dict extractor. Tests cover those plus a regression guard
asserting no lexical-keyword feature has been added back in.
"""
from __future__ import annotations

import unittest

from services.proactive.features import (
    clip01,
    extract_primitive_features,
    log_norm,
    signed_log_norm,
)
from services.proactive.models import ProactiveObservation


class FeatureMathTests(unittest.TestCase):
    def test_clip01_bounds(self) -> None:
        self.assertEqual(clip01(-1.0), 0.0)
        self.assertEqual(clip01(0.5), 0.5)
        self.assertEqual(clip01(1.7), 1.0)

    def test_log_norm_monotonic_and_bounded(self) -> None:
        self.assertEqual(log_norm(0.0, 100.0), 0.0)
        self.assertAlmostEqual(log_norm(100.0, 100.0), 1.0, places=6)
        self.assertLess(log_norm(1.0, 100.0), log_norm(10.0, 100.0))
        self.assertLess(log_norm(10.0, 100.0), log_norm(100.0, 100.0))
        self.assertEqual(log_norm(-50.0, 100.0), 0.0)

    def test_signed_log_norm_keeps_sign(self) -> None:
        self.assertGreater(signed_log_norm(50.0, 1000.0), 0.0)
        self.assertLess(signed_log_norm(-50.0, 1000.0), 0.0)
        self.assertEqual(signed_log_norm(0.0, 1000.0), 0.0)
        self.assertAlmostEqual(
            abs(signed_log_norm(123.0, 1000.0)),
            signed_log_norm(123.0, 1000.0),
            places=6,
        )


class PrimitiveExtractorTests(unittest.TestCase):
    def test_churn_high_when_lots_of_edits_low_net_growth(self) -> None:
        obs = ProactiveObservation(
            surface="native_editor",
            workspace_id="ws",
            document_key="doc",
            text="x" * 800,
            cursor_index=400,
            current_paragraph="x" * 200,
        )
        p = extract_primitive_features(
            observation=obs,
            idle_sec=1.0,
            stable_capture_count=0,
            added_chars_window=200.0,
            deleted_chars_window=180.0,
            recent_negative_rate=0.0,
            time_since_last_intervention=60.0,
            relevant_sources_available=False,
        )
        self.assertGreater(p["churn_score"], 0.3)
        self.assertEqual(p["surface_is_native"], 1.0)

    def test_external_surface_flag(self) -> None:
        obs = ProactiveObservation(
            surface="external_screen",
            workspace_id="ws",
            document_key="doc",
            text="hello",
        )
        p = extract_primitive_features(
            observation=obs,
            idle_sec=10.0,
            stable_capture_count=3,
            added_chars_window=0.0,
            deleted_chars_window=0.0,
            recent_negative_rate=0.0,
            time_since_last_intervention=0.0,
            relevant_sources_available=False,
        )
        self.assertEqual(p["surface_is_native"], 0.0)
        # Cursor unknown → cursor_pos defaults to 1.0.
        self.assertEqual(p["cursor_pos"], 1.0)

    def test_primitive_has_no_lexical_keyword_features(self) -> None:
        """Regression guard for the 2026-05-28 directive: no hard-coded
        keyword-derived features are allowed in the primitive dict."""
        obs = ProactiveObservation(
            surface="native_editor",
            workspace_id="ws",
            document_key="doc",
            text="2024년 통계청 자료에 따르면 35%가 증가했다. 근거가 필요하다.",
            current_paragraph="2024년 통계청 자료에 따르면 35%가 증가했다.",
            current_sentence="근거가 필요하다.",
            cursor_index=30,
        )
        p = extract_primitive_features(
            observation=obs,
            idle_sec=2.0,
            stable_capture_count=1,
            added_chars_window=50.0,
            deleted_chars_window=0.0,
            recent_negative_rate=0.0,
            time_since_last_intervention=60.0,
            relevant_sources_available=False,
        )
        # The dict must not carry any lexical-keyword-derived score, even
        # when the input text would have triggered the old detector.
        self.assertNotIn("evidence_need_score", p)
        for forbidden in ("factual_claim", "needs_citation", "claim_score"):
            self.assertNotIn(forbidden, p)


class NoKeywordModulesTests(unittest.TestCase):
    """Regression guard: the removed lexical-keyword symbols must NOT come
    back. If someone adds a ``_EVIDENCE_KEYWORDS`` / ``_FACTUAL_KEYWORDS``
    tuple anywhere in the proactive package, this fails loudly."""

    def test_features_has_no_keyword_constants(self) -> None:
        import services.proactive.features as f

        for forbidden in (
            "_EVIDENCE_KEYWORDS",
            "_FACTUAL_KEYWORDS",
            "compute_evidence_need",
            "_NUMBER_RE",
            "_YEAR_RE",
            "_PERCENT_RE",
        ):
            self.assertFalse(hasattr(f, forbidden), forbidden)

    def test_candidates_has_no_keyword_constants(self) -> None:
        import services.proactive.candidates as c

        for forbidden in (
            "_FACTUAL_KEYWORDS",
            "_has_factual_claim",
            "_NUMERIC",
            "_YEAR",
            "_PERCENT",
            "_maybe_evidence_or_citation_prompt",
        ):
            self.assertFalse(hasattr(c, forbidden), forbidden)


if __name__ == "__main__":
    unittest.main()
