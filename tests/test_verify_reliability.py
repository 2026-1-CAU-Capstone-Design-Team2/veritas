"""Reliability verdict derivation (services/verification/reliability/llm_judge).

Locks the softened ``request_alignment`` rule: a lone "weak" alignment signal —
which a small judge over-emits by conflating "doesn't fully answer the
deliverable" with "off-topic" — must NOT force the verdict to "low" (the old
hard override classified entire workspaces as 낮음). It now caps at "medium" and
only reaches "low" when a second signal corroborates the weakness.
"""

from __future__ import annotations

import unittest

from services.verification.reliability.llm_judge import _derive_level


def _sig(alignment="mixed", authority="mixed", verifiability="mixed", self_consistency="mixed"):
    return {
        "request_alignment": alignment,
        "authority": authority,
        "verifiability": verifiability,
        "self_consistency": self_consistency,
    }


class DeriveLevelTests(unittest.TestCase):
    def test_lone_weak_alignment_caps_at_medium_not_low(self) -> None:
        # The core fix: weak alignment with otherwise-credible signals → medium.
        self.assertEqual(_derive_level(_sig(alignment="weak"), "low"), "medium")

    def test_weak_alignment_with_strong_others_is_still_only_medium(self) -> None:
        # Off-topic caps the ceiling: even all-strong supporting signals can't
        # lift a weak-alignment doc above medium.
        self.assertEqual(
            _derive_level(
                _sig(alignment="weak", authority="strong", verifiability="strong",
                     self_consistency="strong"),
                "high",
            ),
            "medium",
        )

    def test_weak_alignment_with_one_corroborating_weak_is_low(self) -> None:
        self.assertEqual(
            _derive_level(_sig(alignment="weak", authority="weak"), "medium"),
            "low",
        )

    def test_weak_alignment_with_two_weak_is_low(self) -> None:
        self.assertEqual(
            _derive_level(
                _sig(alignment="weak", authority="weak", verifiability="weak"),
                "low",
            ),
            "low",
        )

    def test_high_requires_two_strong_and_no_weak(self) -> None:
        self.assertEqual(
            _derive_level(
                _sig(alignment="strong", authority="strong", verifiability="strong"),
                "high",
            ),
            "high",
        )

    def test_strong_alignment_but_thin_support_is_medium(self) -> None:
        # Strong alignment alone does not produce high — needs 2 strong supports.
        self.assertEqual(_derive_level(_sig(alignment="strong"), "high"), "medium")

    def test_two_weak_supports_is_low_even_when_on_topic(self) -> None:
        self.assertEqual(
            _derive_level(
                _sig(alignment="mixed", authority="weak", verifiability="weak"),
                "medium",
            ),
            "low",
        )

    def test_all_mixed_is_medium(self) -> None:
        self.assertEqual(_derive_level(_sig(), "medium"), "medium")

    def test_llm_level_is_ignored(self) -> None:
        # The LLM's own (drifting) level never overrides the signal-derived one.
        self.assertEqual(
            _derive_level(
                _sig(alignment="strong", authority="strong", verifiability="strong"),
                "low",  # LLM said low, signals say high → high wins
            ),
            "high",
        )


if __name__ == "__main__":
    unittest.main()
