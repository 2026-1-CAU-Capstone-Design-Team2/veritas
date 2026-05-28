"""DisjointDiscountedLinUCB unit tests.

We check three contracts the orchestrator depends on:

1. The action mask is honored — the policy never selects an action that's
   not in the available set.
2. After repeated positive updates for one arm and negative updates for
   another, the policy prefers the positive arm under any reasonable mask.
3. ``to_payload`` / ``from_payload`` round-trip preserves the predictions
   exactly so saved state can be re-loaded across restarts.
"""
from __future__ import annotations

import random
import unittest

from services.proactive.policies import DisjointDiscountedLinUCB


def _policy() -> DisjointDiscountedLinUCB:
    return DisjointDiscountedLinUCB(
        actions=["a", "b", "c"],
        feature_names=["bias", "f1", "f2"],
        alpha=0.3,
        discount=0.99,
    )


class LinUCBTests(unittest.TestCase):
    def test_select_honors_mask(self) -> None:
        p = _policy()
        result = p.select([1.0, 0.5, 0.2], available_actions=["b", "c"])
        self.assertIn(result["selected"], ("b", "c"))
        self.assertNotIn("a", result["available"])

    def test_select_falls_back_when_mask_empty(self) -> None:
        p = _policy()
        result = p.select([1.0, 0.5, 0.2], available_actions=[])
        # Falls back to all actions, picks something.
        self.assertIn(result["selected"], ("a", "b", "c"))

    def test_select_returns_none_when_mask_outside_action_set(self) -> None:
        p = _policy()
        result = p.select([1.0, 0.0, 0.0], available_actions=["z"])
        self.assertIsNone(result["selected"])

    def test_learns_preferred_arm(self) -> None:
        p = _policy()
        rng = random.Random(7)
        # Two contexts: x_good (f1=1) where arm "a" gets +1, and x_bad
        # (f1=0) where arm "b" gets +1. After enough samples, given x_good
        # the UCB ranking should favor "a".
        for _ in range(60):
            x_good = [1.0, 1.0, rng.uniform(-0.1, 0.1)]
            x_bad = [1.0, 0.0, rng.uniform(-0.1, 0.1)]
            p.update("a", x_good, 1.0)
            p.update("a", x_bad, -1.0)
            p.update("b", x_good, -1.0)
            p.update("b", x_bad, 1.0)

        pick_good = p.select([1.0, 1.0, 0.0])["selected"]
        pick_bad = p.select([1.0, 0.0, 0.0])["selected"]
        self.assertEqual(pick_good, "a")
        self.assertEqual(pick_bad, "b")

    def test_dim_mismatch_raises(self) -> None:
        p = _policy()
        with self.assertRaises(ValueError):
            p.select([1.0, 0.0])  # missing one feature

    def test_payload_round_trip(self) -> None:
        p = _policy()
        rng = random.Random(11)
        for _ in range(20):
            x = [1.0, rng.uniform(0, 1), rng.uniform(0, 1)]
            p.update("a", x, rng.uniform(-1, 1))
            p.update("b", x, rng.uniform(-1, 1))
        payload = p.to_payload()
        q = DisjointDiscountedLinUCB.from_payload(payload)
        x_test = [1.0, 0.7, 0.3]
        r_p = p.select(x_test)
        r_q = q.select(x_test)
        self.assertEqual(r_p["selected"], r_q["selected"])
        for action in r_p["scores"]:
            self.assertAlmostEqual(r_p["scores"][action], r_q["scores"][action], places=6)


if __name__ == "__main__":
    unittest.main()
