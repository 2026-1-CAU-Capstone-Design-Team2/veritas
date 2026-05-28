"""RuleEvaluator tests.

Locks down:
- Hard gates reject off-anchor / unsupported render / cooldown / suppression
- Threshold goes to +inf when on cooldown
- score is in [0, 1]
- Recent negative rate pushes the threshold up
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from services.proactive.anchors import ActiveAnchor
from services.proactive.candidates import PrimitiveSignals
from services.proactive.evaluator import (
    BASE_SHOW_THRESHOLD,
    adjusted_threshold,
    check_hard_gates,
    score_candidate,
)
from services.proactive.proposal_models import ProactiveTask, SurfaceCapabilities


def _anchor() -> ActiveAnchor:
    return ActiveAnchor(
        document_id="doc-x",
        surface="external_app",
        cursor_index=100,
        paragraph_text="이 단락은 충분히 깁니다. 사용자가 무언가를 작성 중입니다." * 3,
        sentence_text="사용자가 무언가를 작성 중입니다.",
        prev_paragraph="이전 단락",
        next_paragraph="다음 단락",
        source="uia_caret",
        confidence=0.85,
    )


def _signals(**overrides) -> PrimitiveSignals:
    s = PrimitiveSignals(
        idle_sec=4.0,
        stable_capture_count=2,
        paragraph_len=180,
        document_len=1000,
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _task(*, task_type: str, anchor_id: str, render: str = "external_card_orange") -> ProactiveTask:
    return ProactiveTask(
        task_type=task_type,  # type: ignore[arg-type]
        target_anchor_id=anchor_id,
        context_scope="current_paragraph",  # type: ignore[arg-type]
        render_mode=render,  # type: ignore[arg-type]
        confidence=0.85,
    )


class HardGateTests(unittest.TestCase):
    def test_off_anchor_target_is_rejected(self) -> None:
        a = _anchor()
        task = _task(task_type="paragraph_rewrite", anchor_id="anc_DIFFERENT")
        result = check_hard_gates(
            candidate=task,
            anchor=a,
            signals=_signals(),
            surface=SurfaceCapabilities.for_external(),
        )
        self.assertFalse(result.allowed)
        self.assertIn("off_anchor_target", result.reasons)

    def test_unsupported_render_is_rejected(self) -> None:
        a = _anchor()
        task = _task(
            task_type="paragraph_rewrite",
            anchor_id=a.anchor_id,
            render="native_inline_diff",  # native renderer on an external card surface
        )
        result = check_hard_gates(
            candidate=task,
            anchor=a,
            signals=_signals(),
            surface=SurfaceCapabilities.for_external(),
        )
        self.assertFalse(result.allowed)
        self.assertIn("surface_render_unsupported", result.reasons)

    def test_anchor_task_cooldown_blocks(self) -> None:
        a = _anchor()
        task = _task(task_type="paragraph_rewrite", anchor_id=a.anchor_id)

        class FakeState:
            anchor_cooldowns = {
                f"{a.anchor_id}|paragraph_rewrite": type(
                    "Cooldown",
                    (),
                    {
                        "cooldown_until": (
                            datetime.now(timezone.utc) + timedelta(seconds=300)
                        )
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "reason": "reject",
                    },
                )()
            }
            task_type_stats: dict = {}
            global_stats = type("G", (), {"recent_negative_rate": 0.0})()
            threshold_offset = 0.0

        result = check_hard_gates(
            candidate=task,
            anchor=a,
            signals=_signals(),
            surface=SurfaceCapabilities.for_external(),
            user_adaptation=FakeState(),
        )
        self.assertFalse(result.allowed)
        self.assertIn("cooldown_same_anchor_task", result.reasons)


class ScoreTests(unittest.TestCase):
    def test_score_within_unit_interval(self) -> None:
        a = _anchor()
        task = _task(task_type="paragraph_rewrite", anchor_id=a.anchor_id)
        breakdown = score_candidate(candidate=task, anchor=a, signals=_signals())
        self.assertGreaterEqual(breakdown.total, 0.0)
        self.assertLessEqual(breakdown.total, 1.0)

    def test_recent_negative_rate_reduces_score(self) -> None:
        a = _anchor()
        task = _task(task_type="paragraph_rewrite", anchor_id=a.anchor_id)
        no_neg = score_candidate(candidate=task, anchor=a, signals=_signals()).total

        class State:
            class global_stats:
                recent_negative_rate = 0.9

            threshold_offset = 0.0
            anchor_cooldowns: dict = {}
            task_type_stats: dict = {}

        high_neg = score_candidate(
            candidate=task,
            anchor=a,
            signals=_signals(),
            user_adaptation=State(),
        ).total
        self.assertLess(high_neg, no_neg)

    def test_native_next_sentence_passes_threshold_at_idle_zero(self) -> None:
        """Regression test for the live-usage issue: native ghost ought to
        clear the threshold even when the orchestrator's idle_sec is 0
        (which it always is — debounce + text-changed resets it)."""
        from services.proactive.anchors import ActiveAnchor

        a = ActiveAnchor(
            document_id="doc-x",
            surface="native_editor",
            cursor_index=60,
            paragraph_text="이 문서는 충분히 긴 본문을 가집니다. 다음 문장을 작성합니다.",
            sentence_text="다음 문장을 작성합니다.",
            source="native_cursor",
            confidence=0.95,
        )
        task = _task(
            task_type="next_sentence",
            anchor_id=a.anchor_id,
            render="native_ghost",
        )
        breakdown = score_candidate(
            candidate=task,
            anchor=a,
            signals=PrimitiveSignals(idle_sec=0.0, stable_capture_count=0, paragraph_len=60),
        )
        # Should comfortably clear the base threshold (0.50 after recalibration).
        self.assertGreaterEqual(breakdown.total, BASE_SHOW_THRESHOLD + 0.10)


class ThresholdTests(unittest.TestCase):
    def test_base_threshold_default(self) -> None:
        t = adjusted_threshold(task_type="next_sentence", anchor_id="anc_x")
        self.assertAlmostEqual(t, BASE_SHOW_THRESHOLD, places=4)

    def test_offset_lowers_threshold_after_accept(self) -> None:
        class State:
            anchor_cooldowns: dict = {}
            task_type_stats: dict = {}
            threshold_offset = -0.05

            class global_stats:
                recent_negative_rate = 0.0

        t = adjusted_threshold(
            task_type="next_sentence", anchor_id="anc_x", user_adaptation=State()
        )
        self.assertAlmostEqual(t, BASE_SHOW_THRESHOLD - 0.05, places=4)

    def test_cooldown_returns_infinity(self) -> None:
        until_iso = (
            datetime.now(timezone.utc) + timedelta(seconds=300)
        ).isoformat().replace("+00:00", "Z")

        class State:
            anchor_cooldowns = {
                "anc_x|paragraph_rewrite": type(
                    "C",
                    (),
                    {"cooldown_until": until_iso, "reason": "reject"},
                )()
            }
            task_type_stats: dict = {}
            threshold_offset = 0.0

            class global_stats:
                recent_negative_rate = 0.0

        t = adjusted_threshold(
            task_type="paragraph_rewrite",
            anchor_id="anc_x",
            user_adaptation=State(),
        )
        self.assertEqual(t, float("inf"))


if __name__ == "__main__":
    unittest.main()
