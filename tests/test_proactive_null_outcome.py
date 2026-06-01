"""Null-outcome classification tests."""
from __future__ import annotations

import unittest

from services.proactive.null_outcome_monitor import classify_null_outcome


class ClassifyNullOutcomeTests(unittest.TestCase):
    def test_continued_writing_is_tn_proxy(self) -> None:
        outcome = classify_null_outcome(
            edit_volume_since=120.0,
            churn_since=0.20,
            idle_since=2.0,
        )
        self.assertEqual(outcome, "tn_proxy")

    def test_idle_stuck_is_fn_proxy(self) -> None:
        outcome = classify_null_outcome(
            edit_volume_since=0.0,
            churn_since=0.10,
            idle_since=40.0,
        )
        self.assertEqual(outcome, "fn_proxy")

    def test_high_churn_is_fn_proxy(self) -> None:
        outcome = classify_null_outcome(
            edit_volume_since=200.0,
            churn_since=0.80,
            idle_since=3.0,
        )
        self.assertEqual(outcome, "fn_proxy")

    def test_explicit_help_request_is_fn_proxy(self) -> None:
        outcome = classify_null_outcome(
            edit_volume_since=0.0,
            churn_since=0.10,
            idle_since=2.0,
            user_invoked_help=True,
        )
        self.assertEqual(outcome, "fn_proxy")

    def test_app_switched_is_unknown(self) -> None:
        outcome = classify_null_outcome(
            edit_volume_since=0.0,
            churn_since=0.10,
            idle_since=10.0,
            app_switched=True,
        )
        self.assertEqual(outcome, "unknown")

    def test_inconclusive_defaults_to_unknown(self) -> None:
        outcome = classify_null_outcome(
            edit_volume_since=10.0,  # too small for TN
            churn_since=0.30,
            idle_since=15.0,
        )
        self.assertEqual(outcome, "unknown")


if __name__ == "__main__":
    unittest.main()
