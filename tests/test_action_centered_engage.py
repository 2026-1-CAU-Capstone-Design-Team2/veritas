"""Action-Centered Engage Policy unit tests.

The contracts we're protecting:

1. State actually moves on update — B and b_hat change in the expected
   direction.
2. The safety gates force π_t to 0 under their declared conditions (which
   means the sampled action is always ``no_op``, regardless of context).
3. After repeated rewards favoring intervene, ``p_positive`` rises; with
   rewards favoring no-op, it falls.
4. The payload round-trip preserves selection behavior.
"""
from __future__ import annotations

import random
import unittest

import numpy as np

from services.proactive.policies import ActionCenteredEngagePolicy


def _policy() -> ActionCenteredEngagePolicy:
    return ActionCenteredEngagePolicy(
        feature_names=["bias", "x1"],
        pi_min=0.05,
        pi_max=0.60,
    )


class EngagePolicyTests(unittest.TestCase):
    def test_update_changes_state(self) -> None:
        p = _policy()
        before_B = np.array(p.to_payload()["B"])
        before_b = np.array(p.to_payload()["b_hat"])
        p.update(x=[1.0, 0.5], engage_action="intervene", pi_t=0.3, reward=1.0)
        after_B = np.array(p.to_payload()["B"])
        after_b = np.array(p.to_payload()["b_hat"])
        # Variance factor is pi*(1-pi) = 0.21 > 0 so B must change.
        self.assertFalse(np.allclose(before_B, after_B))
        # Residual is (1 - 0.3)*1.0 = 0.7 so b_hat moves in +x direction.
        self.assertGreater(after_b[1], before_b[1])

    def test_cooldown_gate_zeroes_pi(self) -> None:
        p = _policy()
        result = p.select(
            [1.0, 0.5],
            candidate_suggestion_type="next_sentence",
            time_since_last_intervention=2.0,
        )
        self.assertEqual(result["pi_t"], 0.0)
        self.assertEqual(result["selected"], "no_op")
        self.assertEqual(result["gate_reason"], "cooldown")

    def test_negative_streak_gate(self) -> None:
        p = _policy()
        result = p.select(
            [1.0, 0.5],
            candidate_suggestion_type="next_sentence",
            time_since_last_intervention=120.0,
            recent_negative_rate=0.95,
            idle_sec=5.0,
        )
        self.assertEqual(result["pi_t"], 0.0)
        self.assertEqual(result["gate_reason"], "negative_streak")

    def test_no_candidate_forces_noop(self) -> None:
        p = _policy()
        result = p.select(
            [1.0, 0.5],
            candidate_suggestion_type=None,
            time_since_last_intervention=120.0,
        )
        self.assertEqual(result["pi_t"], 0.0)
        self.assertEqual(result["gate_reason"], "no_candidate")

    def test_p_positive_rises_when_intervene_pays(self) -> None:
        p = _policy()
        # Simulate 80 decisions: when x[1] is high, intervene paid +1; when
        # low, intervene paid -1. We need p_positive(x_high) > p_positive(x_low).
        rng = random.Random(13)
        for _ in range(80):
            x = [1.0, rng.choice([0.1, 0.9])]
            pi_t = 0.4
            if x[1] > 0.5:
                # The intervention was good when x was high.
                p.update(x=x, engage_action="intervene", pi_t=pi_t, reward=1.0)
                p.update(x=x, engage_action="no_op", pi_t=pi_t, reward=-0.2)
            else:
                p.update(x=x, engage_action="intervene", pi_t=pi_t, reward=-1.0)
                p.update(x=x, engage_action="no_op", pi_t=pi_t, reward=0.2)

        result_high = p.select(
            [1.0, 0.9],
            candidate_suggestion_type="next_sentence",
            time_since_last_intervention=120.0,
        )
        result_low = p.select(
            [1.0, 0.1],
            candidate_suggestion_type="next_sentence",
            time_since_last_intervention=120.0,
        )
        self.assertGreater(result_high["p_positive"], result_low["p_positive"])

    def test_payload_round_trip(self) -> None:
        p = _policy()
        for _ in range(10):
            p.update(x=[1.0, 0.5], engage_action="intervene", pi_t=0.3, reward=1.0)
        payload = p.to_payload()
        q = ActionCenteredEngagePolicy.from_payload(payload)
        rng = random.Random(7)
        a = p.select(
            [1.0, 0.5],
            candidate_suggestion_type="next_sentence",
            time_since_last_intervention=999.0,
            rng=random.Random(7),
        )
        b = q.select(
            [1.0, 0.5],
            candidate_suggestion_type="next_sentence",
            time_since_last_intervention=999.0,
            rng=random.Random(7),
        )
        self.assertAlmostEqual(a["pi_t"], b["pi_t"], places=6)
        self.assertAlmostEqual(a["mean"], b["mean"], places=6)


if __name__ == "__main__":
    unittest.main()
