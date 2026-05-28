"""UserAdaptationMemory tests.

The asymmetry of feedback rules is what makes this rule-based system
behave well, so we lock it down explicitly:

- accept   → mildly lowers threshold_offset, may clear cooldown
- reject   → raises threshold_offset, sets anchor cooldown
- retry    → does NOT raise threshold; sets prompt_style flags
- timeout  → small threshold bump + short anchor cooldown
- wrong_anchor → small threshold bump; does NOT count as task_type rejection
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from services.proactive.adaptation import (
    ADAPTATION_FILE,
    TASK_TYPE_REJECTS_FOR_SUPPRESSION,
    UserAdaptationMemory,
)


def _build(tmp: Path, ws: str = "ws_adapt") -> UserAdaptationMemory:
    return UserAdaptationMemory(workspace_dir=tmp / ws, workspace_id=ws)


class AdaptationTests(unittest.TestCase):
    def test_accept_lowers_threshold_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = _build(Path(tmp))
            before = mem.state.threshold_offset
            mem.apply_feedback(canonical="accept", task_type="next_sentence", anchor_id="anc_a")
            after = mem.state.threshold_offset
            self.assertLess(after, before)
            self.assertEqual(mem.state.task_type_stats["next_sentence"].accept, 1)

    def test_reject_raises_threshold_and_sets_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = _build(Path(tmp))
            mem.apply_feedback(canonical="reject", task_type="paragraph_rewrite", anchor_id="anc_b")
            self.assertGreater(mem.state.threshold_offset, 0.0)
            self.assertIn("anc_b|paragraph_rewrite", mem.state.anchor_cooldowns)
            self.assertEqual(mem.state.task_type_stats["paragraph_rewrite"].reject, 1)

    def test_repeated_reject_suppresses_task_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = _build(Path(tmp))
            for i in range(TASK_TYPE_REJECTS_FOR_SUPPRESSION):
                mem.apply_feedback(
                    canonical="reject",
                    task_type="paragraph_rewrite",
                    anchor_id=f"anc_{i}",
                    # External surface — task-type suppression IS active here.
                    surface="external_screen",
                )
            self.assertIsNotNone(
                mem.state.task_type_stats["paragraph_rewrite"].suppressed_until
            )

    def test_native_rejects_do_not_trigger_task_type_suppression(self) -> None:
        """Per services/proactive/README.md §3.1: native rejects must NOT
        contribute to ``same_task_recently_rejected`` — only the in-memory
        per-anchor ladder gates native. Moving to a fresh anchor after 3
        rejects elsewhere should still see a clean task type."""
        with tempfile.TemporaryDirectory() as tmp:
            mem = _build(Path(tmp))
            # Way past the global threshold of 5 rejects, but all on native.
            for i in range(10):
                mem.apply_feedback(
                    canonical="reject",
                    task_type="next_sentence",
                    anchor_id=f"anc_{i}",
                    surface="native_editor",
                )
            stats = mem.state.task_type_stats.get("next_sentence")
            self.assertIsNotNone(stats)
            self.assertIsNone(
                stats.suppressed_until,
                "native rejects must not trigger global task-type suppression",
            )
            self.assertEqual(
                stats.recent_reject_iso, [],
                "native rejects must not accumulate in the global reject ring",
            )

    def test_legacy_persisted_reject_ring_is_gc_ed_on_load(self) -> None:
        """Pre-fix sessions wrote native rejects into ``recent_reject_iso``;
        those entries would otherwise live forever (native never writes
        more, so runtime GC never fires for that bucket). Load must clean
        up stale rows."""
        with tempfile.TemporaryDirectory() as tmp:
            mem1 = _build(Path(tmp), ws="legacy_ws")
            # Inject 10 stale ISOs from 30+ minutes ago to simulate
            # what an old user_adaptation.json would carry.
            stats = mem1._ensure_task_stats("next_sentence")
            from datetime import datetime, timedelta, timezone

            stale_iso = (
                datetime.now(timezone.utc) - timedelta(minutes=60)
            ).isoformat().replace("+00:00", "Z")
            stats.recent_reject_iso = [stale_iso] * 10
            stats.suppressed_until = (
                datetime.now(timezone.utc) - timedelta(minutes=10)
            ).isoformat().replace("+00:00", "Z")  # also stale
            mem1.save()

            # Reopen — load should GC.
            mem2 = _build(Path(tmp), ws="legacy_ws")
            stats2 = mem2.state.task_type_stats.get("next_sentence")
            self.assertIsNotNone(stats2)
            self.assertEqual(stats2.recent_reject_iso, [])
            self.assertIsNone(stats2.suppressed_until)

    def test_retry_does_not_raise_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = _build(Path(tmp))
            before = mem.state.threshold_offset
            mem.apply_feedback(canonical="retry", task_type="next_sentence", anchor_id="anc_a")
            after = mem.state.threshold_offset
            self.assertEqual(after, before)
            self.assertTrue(mem.state.prompt_style_flags.get("prefer_shorter"))
            self.assertEqual(mem.state.task_type_stats["next_sentence"].retry, 1)

    def test_wrong_anchor_does_not_count_as_task_reject(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = _build(Path(tmp))
            mem.apply_feedback(
                canonical="wrong_anchor",
                task_type="logic_flow_review",
                anchor_id="anc_z",
            )
            stats = mem.state.task_type_stats["logic_flow_review"]
            self.assertEqual(stats.reject, 0, "wrong_anchor must not bump task reject")
            self.assertEqual(stats.wrong_anchor, 1)
            # No anchor cooldown was set — wrong_anchor blames extraction, not
            # the user's preference.
            self.assertNotIn(
                "anc_z|logic_flow_review", mem.state.anchor_cooldowns
            )

    def test_accept_clears_matching_anchor_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = _build(Path(tmp))
            mem.apply_feedback(
                canonical="reject", task_type="paragraph_rewrite", anchor_id="anc_c"
            )
            self.assertIn("anc_c|paragraph_rewrite", mem.state.anchor_cooldowns)
            mem.apply_feedback(
                canonical="accept", task_type="paragraph_rewrite", anchor_id="anc_c"
            )
            self.assertNotIn("anc_c|paragraph_rewrite", mem.state.anchor_cooldowns)

    def test_persists_to_atomic_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = _build(Path(tmp))
            mem.apply_feedback(
                canonical="reject", task_type="paragraph_rewrite", anchor_id="anc_d"
            )
            path = Path(tmp) / "ws_adapt" / "proactive_policy" / ADAPTATION_FILE
            self.assertTrue(path.exists())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertGreaterEqual(data["threshold_offset"], 0.0)
            self.assertIn("anc_d|paragraph_rewrite", data["anchor_cooldowns"])
            self.assertEqual(data["task_type_stats"]["paragraph_rewrite"]["reject"], 1)

    def test_reset_clears_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = _build(Path(tmp))
            mem.apply_feedback(
                canonical="reject", task_type="paragraph_rewrite", anchor_id="anc_d"
            )
            mem.reset()
            self.assertEqual(mem.state.threshold_offset, 0.0)
            self.assertEqual(mem.state.anchor_cooldowns, {})
            self.assertEqual(mem.state.task_type_stats, {})


if __name__ == "__main__":
    unittest.main()
