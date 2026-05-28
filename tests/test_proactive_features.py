"""Unit tests for the proactive feature math + primitive extractor."""
from __future__ import annotations

import math
import unittest

from services.proactive.features import (
    ENGAGE_FEATURE_NAMES,
    SUGGEST_FEATURE_NAMES,
    build_engage_features,
    build_suggest_features,
    clip01,
    compute_evidence_need,
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
        # Cap value should produce 1.0 exactly.
        self.assertAlmostEqual(log_norm(100.0, 100.0), 1.0, places=6)
        # Strictly monotonic.
        self.assertLess(log_norm(1.0, 100.0), log_norm(10.0, 100.0))
        self.assertLess(log_norm(10.0, 100.0), log_norm(100.0, 100.0))
        # Negative inputs clamp to 0.
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

    def test_evidence_score_scales_with_signals(self) -> None:
        empty = compute_evidence_need(sentence="", paragraph="")
        weak = compute_evidence_need(sentence="이건 그냥 의견이다.", paragraph="짧은 단락")
        strong = compute_evidence_need(
            sentence="2024년 통계에 따르면 35%가 증가했다는데 근거는?",
            paragraph="2024년 통계 자료에서 35% 증가가 관측된다는 보고가 있다. 출처가 필요하다.",
        )
        self.assertEqual(empty, 0.0)
        self.assertGreater(strong, weak)
        self.assertLessEqual(strong, 1.0)


class FeatureVectorShapeTests(unittest.TestCase):
    def _primitive(self, **overrides) -> dict:
        base = {
            "idle_sec": 3.0,
            "stable_capture_count": 2,
            "added_chars_window": 30.0,
            "deleted_chars_window": 5.0,
            "edit_volume": 35.0,
            "net_growth": 25.0,
            "churn_score": 0.05,
            "paragraph_len": 120.0,
            "document_len": 1200.0,
            "cursor_pos": 0.5,
            "evidence_need_score": 0.2,
            "relevant_sources_available": True,
            "recent_negative_rate": 0.1,
            "time_since_last_intervention": 30.0,
            "surface_is_native": 1.0,
        }
        base.update(overrides)
        return base

    def test_engage_vector_dimension(self) -> None:
        x = build_engage_features(self._primitive())
        self.assertEqual(len(x), len(ENGAGE_FEATURE_NAMES))
        self.assertEqual(x[0], 1.0)  # bias

    def test_suggest_vector_dimension(self) -> None:
        x = build_suggest_features(self._primitive())
        self.assertEqual(len(x), len(SUGGEST_FEATURE_NAMES))
        self.assertEqual(x[0], 1.0)  # bias

    def test_engage_features_in_unit_box_or_known_range(self) -> None:
        x = build_engage_features(self._primitive(idle_sec=999.0, edit_volume=99999.0))
        for name, value in zip(ENGAGE_FEATURE_NAMES, x):
            if name == "bias":
                continue
            self.assertGreaterEqual(value, 0.0, name)
            self.assertLessEqual(value, 1.0, name)

    def test_suggest_features_signed_growth_in_minus_one_plus_one(self) -> None:
        x_pos = build_suggest_features(self._primitive(net_growth=500.0))
        x_neg = build_suggest_features(self._primitive(net_growth=-500.0))
        # net_growth_signed_norm is the second entry (after bias).
        self.assertGreater(x_pos[1], 0.0)
        self.assertLess(x_neg[1], 0.0)
        self.assertAlmostEqual(x_pos[1], -x_neg[1], places=6)


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
        # Cursor unknown → cursor_pos defaults to 1.0 per spec.
        self.assertEqual(p["cursor_pos"], 1.0)


if __name__ == "__main__":
    unittest.main()
