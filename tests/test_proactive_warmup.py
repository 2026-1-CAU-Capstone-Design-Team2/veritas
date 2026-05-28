"""Cold-start warmup tests for the engage policy.

The user's failure mode that motivated the warmup:
  1. Initial π_t = 0.50 (Φ(0) clipped to [pi_min, pi_max]).
  2. 3 strong rejects in a row drive p_positive below 0.05.
  3. π_t locks at pi_min=0.05 — the user sees almost no suggestions.

The warmup gate breaks this: for the first ``warmup_decisions`` select()
calls we hold π_t at ``warmup_pi_floor`` regardless of the learned mean.
This gives the policy enough rollouts to *learn what works*, not just
"the first 3 random tries were bad — give up forever."
"""
from __future__ import annotations

import random
import unittest

from services.proactive.policies import ActionCenteredEngagePolicy


def _policy(**kwargs) -> ActionCenteredEngagePolicy:
    defaults = dict(
        feature_names=["bias", "x1"],
        pi_min=0.05,
        pi_max=0.60,
        warmup_decisions=20,
        warmup_pi_floor=0.30,
    )
    defaults.update(kwargs)
    return ActionCenteredEngagePolicy(**defaults)


class WarmupTests(unittest.TestCase):
    def test_floor_holds_after_three_strong_rejects(self) -> None:
        """The exact failure mode the user reported."""
        p = _policy()
        # Hit the policy with 3 strong negative rewards under intervene —
        # this is what would happen if the user rejects three suggestions.
        for _ in range(3):
            res = p.select(
                [1.0, 0.5],
                candidate_suggestion_type="next_sentence",
                time_since_last_intervention=120.0,
                rng=random.Random(7),
            )
            self.assertGreaterEqual(res["pi_t"], 0.30 - 1e-9)
            p.update(
                x=[1.0, 0.5],
                engage_action="intervene",
                pi_t=res["pi_t"],
                reward=-1.0,
            )
        # Still inside warmup window — pi_t should still be at the floor,
        # not collapsed to pi_min=0.05.
        res4 = p.select(
            [1.0, 0.5],
            candidate_suggestion_type="next_sentence",
            time_since_last_intervention=120.0,
            rng=random.Random(7),
        )
        self.assertGreaterEqual(res4["pi_t"], 0.30 - 1e-9)
        self.assertTrue(res4["warmup_active"])
        self.assertGreater(res4["warmup_remaining"], 0)

    def test_warmup_count_increments_on_every_select(self) -> None:
        p = _policy(warmup_decisions=5)
        rng = random.Random(1)
        for i in range(7):
            res = p.select(
                [1.0, 0.0],
                candidate_suggestion_type="next_sentence",
                time_since_last_intervention=120.0,
                rng=rng,
            )
            if i < 5:
                self.assertTrue(res["warmup_active"], f"i={i}")
            else:
                self.assertFalse(res["warmup_active"], f"i={i}")
        self.assertEqual(p._total_decisions, 7)

    def test_warmup_does_not_override_safety_gates(self) -> None:
        """Cooldown / negative-streak / no-candidate gates still force pi_t=0
        even inside the warmup window — warmup is forced *exploration*, not a
        bypass of the safety rules."""
        p = _policy()
        cooldown = p.select(
            [1.0, 0.5],
            candidate_suggestion_type="next_sentence",
            time_since_last_intervention=2.0,
            rng=random.Random(1),
        )
        self.assertEqual(cooldown["pi_t"], 0.0)
        self.assertEqual(cooldown["gate_reason"], "cooldown")

        no_cand = p.select(
            [1.0, 0.5],
            candidate_suggestion_type=None,
            time_since_last_intervention=120.0,
            rng=random.Random(1),
        )
        self.assertEqual(no_cand["pi_t"], 0.0)
        self.assertEqual(no_cand["gate_reason"], "no_candidate")

    def test_warmup_count_survives_payload_round_trip(self) -> None:
        p = _policy(warmup_decisions=10)
        for _ in range(7):
            p.select(
                [1.0, 0.5],
                candidate_suggestion_type="next_sentence",
                time_since_last_intervention=120.0,
                rng=random.Random(1),
            )
        payload = p.to_payload()
        q = ActionCenteredEngagePolicy.from_payload(payload)
        self.assertEqual(q._total_decisions, 7)
        self.assertEqual(q.warmup_decisions, 10)
        # Three more decisions should exit the warmup window.
        for i in range(3):
            res = q.select(
                [1.0, 0.5],
                candidate_suggestion_type="next_sentence",
                time_since_last_intervention=120.0,
                rng=random.Random(1),
            )
        post_warmup = q.select(
            [1.0, 0.5],
            candidate_suggestion_type="next_sentence",
            time_since_last_intervention=120.0,
            rng=random.Random(1),
        )
        self.assertFalse(post_warmup["warmup_active"])

    def test_warmup_disabled_via_zero(self) -> None:
        """``warmup_decisions=0`` reproduces the pre-warmup behavior — useful
        when the operator wants the strict spec behavior for offline eval."""
        p = _policy(warmup_decisions=0)
        res = p.select(
            [1.0, 0.5],
            candidate_suggestion_type="next_sentence",
            time_since_last_intervention=120.0,
            rng=random.Random(1),
        )
        self.assertFalse(res["warmup_active"])
        # pi_t comes purely from Thompson — should equal min(pi_max, max(pi_min, 0.5))
        # = 0.5 at zero-state.
        self.assertAlmostEqual(res["pi_t"], 0.5, places=6)


if __name__ == "__main__":
    unittest.main()
