"""Surface ↔ canonical feedback ↔ reward mapping tests.

The contract being protected: a TAB on the native editor and a copy button on
an external card both pay the same engage reward AND the same suggestion
reward. If a refactor breaks that the bandit will silently start learning a
phantom surface effect."""
from __future__ import annotations

import unittest

from services.proactive.reward import (
    CANONICAL_REWARD,
    canonicalize_feedback,
    describe_feedback,
    reward_for,
)


class CanonicalMappingTests(unittest.TestCase):
    def test_native_tab_equals_external_copy(self) -> None:
        native = canonicalize_feedback(surface="native_editor", raw_action="tab")
        external = canonicalize_feedback(surface="external_screen", raw_action="copy")
        self.assertEqual(native, "accept")
        self.assertEqual(external, "accept")
        self.assertEqual(reward_for(native), reward_for(external))

    def test_native_esc_equals_external_red_reject(self) -> None:
        native = canonicalize_feedback(surface="native_editor", raw_action="esc")
        external = canonicalize_feedback(surface="external_screen", raw_action="red_reject")
        self.assertEqual(native, "reject")
        self.assertEqual(external, "reject")
        self.assertEqual(reward_for(native), reward_for(external))

    def test_retry_collapses(self) -> None:
        # Both surfaces accept canonical `retry`. Per spec §5.3 each surface
        # also has its own retry alias — `rewrite` is native-only, `regenerate`
        # is external-only — so we test each on its own surface.
        for surface in ("native_editor", "external_screen"):
            with self.subTest(surface=surface, raw="retry"):
                self.assertEqual(
                    canonicalize_feedback(surface=surface, raw_action="retry"),
                    "retry",
                )
        self.assertEqual(
            canonicalize_feedback(surface="native_editor", raw_action="rewrite"),
            "retry",
        )
        self.assertEqual(
            canonicalize_feedback(surface="external_screen", raw_action="regenerate"),
            "retry",
        )

    def test_legacy_like_aliases_accept(self) -> None:
        # Backwards-compat: pre-bandit clients still send "like"; per §5.3 the
        # canonical layer treats it as accept (without minting a new reward).
        for surface in ("native_editor", "external_screen"):
            self.assertEqual(
                canonicalize_feedback(surface=surface, raw_action="like"),
                "accept",
            )

    def test_timeout_when_unknown(self) -> None:
        self.assertEqual(
            canonicalize_feedback(surface="native_editor", raw_action="cucumber"),
            "timeout",
        )

    def test_noop_canonical_passes_through_synthetic_surface(self) -> None:
        # The orchestrator's no-op outcome monitor uses surface="" to push
        # ``noop_positive`` / ``noop_negative`` straight through.
        self.assertEqual(
            canonicalize_feedback(surface="", raw_action="noop_positive"),
            "noop_positive",
        )
        self.assertEqual(
            canonicalize_feedback(surface="", raw_action="noop_negative"),
            "noop_negative",
        )

    def test_reward_for_unknown_returns_none_pair(self) -> None:
        self.assertEqual(reward_for("unknown"), (None, None))

    def test_canonical_table_complete(self) -> None:
        for k in (
            "accept",
            "reject",
            "retry",
            "timeout",
            "cancelled",
            "wrong_anchor",
            "noop_positive",
            "noop_negative",
        ):
            self.assertIn(k, CANONICAL_REWARD)

    def test_wrong_anchor_round_trip_both_surfaces(self) -> None:
        # New rule-based feedback. Both surfaces must accept it.
        for surface in ("native_editor", "external_screen"):
            with self.subTest(surface=surface):
                self.assertEqual(
                    canonicalize_feedback(surface=surface, raw_action="wrong_anchor"),
                    "wrong_anchor",
                )
                self.assertEqual(
                    canonicalize_feedback(surface=surface, raw_action="off_target"),
                    "wrong_anchor",
                )

    def test_describe_feedback_round_trip(self) -> None:
        d = describe_feedback(surface="native_editor", raw_action="TAB")
        self.assertEqual(d["canonical"], "accept")
        self.assertEqual(d["engage_reward"], 1.0)
        self.assertEqual(d["suggestion_reward"], 1.0)


if __name__ == "__main__":
    unittest.main()
