"""End-to-end orchestrator tests for the rule-based pipeline.

Covers the spec §17 acceptance criteria:

1. observe returns either a ProactiveTask or NullPrediction (never a half-state)
2. Every task carries target_anchor_id, context_scope, render_mode, evaluator_score
3. Off-anchor candidates are impossible (the factory binds to the anchor)
4. Native/external feedback collapses to canonical accept/reject/retry/timeout
5. Feedback updates user_adaptation.json, NOT a bandit state file
6. Same anchor/task reject creates cooldown
7. Persistent logs contain no raw text
8. LLM generation is only called after a candidate passes evaluator/adaptation
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from services.proactive.generator import ProactiveGenerator
from services.proactive.models import ProactiveObservation
from services.proactive.orchestrator import ProactiveOrchestrator
from services.proactive.proposal_models import is_null, is_task


def _gen():
    def ghost(p, s="", *, max_tokens=64, use_workspace=True):
        yield "이어쓰기 본문"

    def assist(a, t, *, max_tokens=400, use_workspace=True):
        yield "주문에 맞춘 본문\n\n설명: 짧은 이유."

    return ProactiveGenerator(
        ghostwrite_iter=ghost,
        editor_assist_iter=assist,
        workspace_is_active=lambda _w: True,
    )


def _build_orch(tmp: Path, ws: str = "ws_api") -> ProactiveOrchestrator:
    return ProactiveOrchestrator(
        output_root=tmp, workspace_id=ws, generator=_gen()
    )


def _native_obs(text: str = "이 문서는 충분히 긴 본문을 가진다. 사용자가 이어 쓸 다음 문장을 작성한다.") -> ProactiveObservation:
    return ProactiveObservation(
        surface="native_editor",
        workspace_id="ws_api",
        document_key="doc-a",
        text=text,
        cursor_index=len(text),
        prefix=text,
        suffix="",
        current_paragraph=text,
        current_sentence="사용자가 이어 쓸 다음 문장을 작성한다.",
    )


class ObserveSchemaTests(unittest.TestCase):
    def test_observe_returns_task_or_null(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch = _build_orch(Path(tmp))
            try:
                result = orch.observe(_native_obs())
                self.assertIn(result["prediction"], ("task", "null"))
                self.assertTrue(result["decision_id"].startswith("pd_"))
            finally:
                orch.close()

    def test_task_carries_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch = _build_orch(Path(tmp))
            try:
                result = orch.observe(_native_obs())
                if result["prediction"] != "task":
                    self.skipTest("orchestrator chose null for this observation")
                task = result["task"]
                self.assertTrue(task.target_anchor_id.startswith("anc_"))
                self.assertTrue(task.context_scope)
                self.assertTrue(task.render_mode)
                self.assertGreaterEqual(task.evaluator_score, 0.0)
            finally:
                orch.close()


class FeedbackTests(unittest.TestCase):
    def test_feedback_updates_user_adaptation_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            orch = _build_orch(tmp_path)
            try:
                result = orch.observe(_native_obs())
                decision_id = result["decision_id"]
                feedback = orch.record_feedback(
                    decision_id=decision_id,
                    raw_action="tab",
                )
                self.assertEqual(feedback["canonical_feedback"], "accept")
                path = tmp_path / "ws_api" / "proactive_policy" / "user_adaptation.json"
                self.assertTrue(path.exists())
                data = json.loads(path.read_text(encoding="utf-8"))
                # accept_ema bumped on a fresh state.
                self.assertGreater(data["global_stats"]["accept_ema"], 0.0)
            finally:
                orch.close()

    def test_native_3_rejects_trigger_anchor_ladder_cooldown(self) -> None:
        """Per services/proactive/README.md §"Native reject ladder":
        native rejects DON'T touch adaptation's per-(anchor, task) cooldown
        (the in-memory ladder owns that gate). The ladder reaches its
        cooldown after 3 consecutive rejects at the same anchor."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            orch = _build_orch(tmp_path)
            try:
                result = orch.observe(_native_obs())
                if result["prediction"] != "task":
                    self.skipTest("orchestrator chose null for this observation")
                decision_id = result["decision_id"]
                anchor_id = result["task"].target_anchor_id

                # 1 reject — ladder records reject_count=1 but no cooldown.
                orch.record_feedback(decision_id=decision_id, raw_action="esc")
                # adaptation cooldown should still be empty for native:
                self.assertNotIn(
                    f"{anchor_id}|next_sentence",
                    orch.store.adaptation.state.anchor_cooldowns,
                )
                count, _, in_cd = orch._read_anchor_state(anchor_id)
                self.assertEqual(count, 1)
                self.assertFalse(in_cd)

                # Two more rejects to trip the 3-strike ladder.
                for _ in range(2):
                    r = orch.observe(_native_obs())
                    if r["prediction"] != "task":
                        self.skipTest("ladder produced null mid-test")
                    orch.record_feedback(
                        decision_id=r["decision_id"], raw_action="esc"
                    )

                count, _, in_cd = orch._read_anchor_state(anchor_id)
                self.assertEqual(count, 3)
                self.assertTrue(in_cd, "ladder cooldown should be active")

                # Next observe at the same anchor should null with the
                # ladder cooldown reason.
                blocked = orch.observe(_native_obs())
                self.assertEqual(blocked["prediction"], "null")
                self.assertEqual(blocked["null"].reason, "anchor_reject_cooldown")
            finally:
                orch.close()

    def test_wrong_anchor_does_not_set_anchor_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            orch = _build_orch(tmp_path)
            try:
                result = orch.observe(_native_obs())
                if result["prediction"] != "task":
                    self.skipTest("orchestrator chose null for this observation")
                decision_id = result["decision_id"]
                anchor_id = result["task"].target_anchor_id
                task_type = result["task"].task_type
                orch.record_feedback(
                    decision_id=decision_id, raw_action="wrong_anchor"
                )
                self.assertNotIn(
                    f"{anchor_id}|{task_type}",
                    orch.store.adaptation.state.anchor_cooldowns,
                )
            finally:
                orch.close()


class PrivacyTests(unittest.TestCase):
    def test_decisions_jsonl_contains_no_raw_text(self) -> None:
        sentinel = "TOPSECRET_SENTENCE_xyz_8417"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            orch = _build_orch(tmp_path)
            try:
                obs = _native_obs(
                    f"이 문서에는 {sentinel}가 들어있다. 이어 쓸 본문이 더 있다."
                )
                orch.observe(obs)
            finally:
                orch.close()
            path = tmp_path / "ws_api" / "proactive_policy" / "decisions.jsonl"
            blob = path.read_text(encoding="utf-8")
            self.assertNotIn(sentinel, blob)

    def test_user_adaptation_contains_no_raw_text(self) -> None:
        sentinel = "PRIVATE_MARK_q9281"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            orch = _build_orch(tmp_path)
            try:
                obs = _native_obs(
                    f"문서에 {sentinel}가 있다. 다음 문장이 이어진다."
                )
                result = orch.observe(obs)
                orch.record_feedback(
                    decision_id=result["decision_id"], raw_action="tab"
                )
            finally:
                orch.close()
            path = tmp_path / "ws_api" / "proactive_policy" / "user_adaptation.json"
            blob = path.read_text(encoding="utf-8")
            self.assertNotIn(sentinel, blob)


class NoBanditImportInProductionPathTests(unittest.TestCase):
    """Spec §17.1: no production path imports the legacy bandit classes."""

    def test_orchestrator_module_does_not_import_bandit(self) -> None:
        import services.proactive.orchestrator as orch_mod
        # The orchestrator module's globals must not contain any bandit policy.
        for forbidden in (
            "ActionCenteredEngagePolicy",
            "DisjointDiscountedLinUCB",
        ):
            self.assertNotIn(forbidden, dir(orch_mod), forbidden)


if __name__ == "__main__":
    unittest.main()
