"""Surface ↔ canonical feedback mapping tests.

The contract being protected: a TAB on the native editor and a copy button on
an external card both collapse to the same ``accept``. If a refactor breaks
that the adaptation layer will silently start learning a phantom surface
effect."""
from __future__ import annotations

import unittest

from services.proactive.reward import canonicalize_feedback


class CanonicalMappingTests(unittest.TestCase):
    def test_native_tab_equals_external_copy(self) -> None:
        self.assertEqual(canonicalize_feedback(surface="native_editor", raw_action="tab"), "accept")
        self.assertEqual(canonicalize_feedback(surface="external_screen", raw_action="copy"), "accept")

    def test_native_esc_equals_external_red_reject(self) -> None:
        self.assertEqual(canonicalize_feedback(surface="native_editor", raw_action="esc"), "reject")
        self.assertEqual(canonicalize_feedback(surface="external_screen", raw_action="red_reject"), "reject")

    def test_retry_collapses(self) -> None:
        # Both surfaces accept canonical ``retry``. Each surface also has its
        # own alias — ``rewrite`` is native-only, ``regenerate`` is external-only.
        for surface in ("native_editor", "external_screen"):
            with self.subTest(surface=surface, raw="retry"):
                self.assertEqual(canonicalize_feedback(surface=surface, raw_action="retry"), "retry")
        self.assertEqual(canonicalize_feedback(surface="native_editor", raw_action="rewrite"), "retry")
        self.assertEqual(canonicalize_feedback(surface="external_screen", raw_action="regenerate"), "retry")

    def test_legacy_like_aliases_accept(self) -> None:
        # Backwards-compat: pre-pivot clients still send "like" — collapses to accept.
        for surface in ("native_editor", "external_screen"):
            self.assertEqual(canonicalize_feedback(surface=surface, raw_action="like"), "accept")

    def test_timeout_when_unknown(self) -> None:
        self.assertEqual(canonicalize_feedback(surface="native_editor", raw_action="cucumber"), "timeout")

    def test_noop_canonical_passes_through_synthetic_surface(self) -> None:
        # null_outcome_monitor pushes ``noop_positive`` / ``noop_negative``
        # straight through using an empty surface name.
        self.assertEqual(canonicalize_feedback(surface="", raw_action="noop_positive"), "noop_positive")
        self.assertEqual(canonicalize_feedback(surface="", raw_action="noop_negative"), "noop_negative")

    def test_wrong_anchor_round_trip_both_surfaces(self) -> None:
        for surface in ("native_editor", "external_screen"):
            with self.subTest(surface=surface):
                self.assertEqual(canonicalize_feedback(surface=surface, raw_action="wrong_anchor"), "wrong_anchor")
                self.assertEqual(canonicalize_feedback(surface=surface, raw_action="off_target"), "wrong_anchor")


if __name__ == "__main__":
    unittest.main()
